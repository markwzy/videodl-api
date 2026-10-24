# VideoDL API (Mac + Docker)

基于 [CharlesPikachu/videodl](https://github.com/CharlesPikachu/videodl) / `videofetch==0.9.1`，并使用 `yt-dlp==2026.10.21` 解析 X / Twitter。可以直接粘贴带文案的分享链接，返回视频标题、封面、媒体 URL、音频 URL 等。抖音和 X 支持通过本地服务流式下载到浏览器，**服务器不保存视频文件**。

## 1. 启动

确保 Mac 上 Docker Desktop 已启动，并在本目录执行：

```bash
docker compose up -d --build
```

- UI: http://localhost:8000
- Swagger: http://localhost:8000/docs
- 健康检查: http://localhost:8000/api/health

首次构建需要从 Docker Hub、Debian 和 PyPI 下载依赖，镜像包含 Chromium、FFmpeg、Node.js，体积可能较大。

## 2. 调用 API

```bash
curl -X POST 'http://localhost:8000/api/parse' \
  -H 'Content-Type: application/json' \
  -d '{"text":"分享视频：https://www.bilibili.com/video/BV..."}'
```

请求体：`{"text":"<视频链接或者整段分享文案>"}`。响应字段：

- `success`：是否拿到有效媒体地址
- `input_url`：提取的原始 URL
- `count`：有效结果数（最多 20 条）
- `items[]`：每项包含 `title`、`source`、`identifier`、`cover_url`、`download_url`、`audio_download_url`、`ext`、`is_hls`、`requires_merge`、`may_require_headers`
- `message`：解析状态说明
- `items[].local_download_url`：抖音和 X 视频的本地下载入口，页面显示为“下载视频”。凭证 10 分钟有效，重启服务或过期后需重新解析。

部分 `download_url` 是限时 URL、m3u8，或者需要携带视频站点请求头/Cookie。解析出 URL **不等于**浏览器可以直接保存 MP4。

抖音通过容器内的 Chromium 打开视频页，读取网页正常发出的作品详情响应，并校验作品 ID 后返回播放地址。支持单条视频链接和短分享链接，不扫描页面中的宣传视频。当前不传入登录 Cookie；需要登录、验证或已经失效的作品可能无法解析。未取得有效媒体地址时，接口返回 HTTP 502 和失败原因。

X / Twitter 使用专用提取器访问官方接口，支持 `x.com`、`twitter.com` 的推文链接和一条推文中的多个视频，选择包含音画的 HTTPS MP4。需要登录的推文必须配置有访问权限的账号登录文件，不会交给第三方解析站点或通用页面扫描。

原始媒体地址在浏览器里直接打开可能返回 403。请点击“下载视频”，由本地服务保留媒体访问所需的请求头，边读取边发送到浏览器；支持单段范围请求和断点续传，不会先下载完整文件或将其装入内存。下载入口只接受本次解析生成的凭证，每次重定向也限定在原平台的媒体域名内。

## 3. 停止、启动和查看日志

```bash
docker compose stop         # 暂停容器
docker compose start        # 重启容器
docker compose logs -f      # 日志
docker compose down         # 停止并删除容器（保留镜像）
```

## 4. 可选配置

默认 `VIDEO_SOURCES` 启用 11 个平台，其中 `DouyinVideoClient` 启用抖音浏览器解析，`TwitterVideoClient` 启用 X 专用提取器，其余平台使用上游专用解析器和页面抓取。需要其他平台可编辑 `compose.yaml` 中的 `VIDEO_SOURCES`，使用上游注册的准确解析器名称。

设置 `VIDEO_SOURCES: "*"` 会使用 **所有**平台注册解析器和通用解析器。**隐私提示**：通用解析器可能把你粘贴的 URL 发给第三方视频解析站点，因而默认不开启。

容器需访问视频站点。Mac 上的本地 HTTP 代理若监听 `127.0.0.1:7890`，该地址在容器里不指向 Mac；可以根据需要在 compose 中配置 `HTTP_PROXY: http://host.docker.internal:7890`、`HTTPS_PROXY: http://host.docker.internal:7890`。前提是代理软件允许从容器连接，且需要留意访问和隐私权限。

### X 登录文件

未设置 `X_COOKIES_FILE` 时以匿名方式解析。需要登录的推文，可将有权访问该内容的账号导出为 Netscape 格式的 `cookies.txt`，保存在本机 `.secrets/x-cookies.txt`，通过本机专用的 `compose.override.yaml` 挂载：

```yaml
services:
  videodl-api:
    environment:
      X_COOKIES_FILE: /run/secrets/x-cookies.txt
    volumes:
      - type: bind
        source: ./.secrets/x-cookies.txt
        target: /run/secrets/x-cookies.txt
        read_only: true
        bind:
          create_host_path: false
```

`.secrets` 目录使用 `0700` 权限，只允许本机当前用户进入；文件使用 `0444`，让容器普通用户通过单文件只读挂载读取。目录、登录文件和本机覆盖配置已排除出镜像构建及版本控制。提取器每次使用独立的临时副本，完成或失败后删除，不会改写原件；账号 Cookie 不会进入 API 响应或媒体下载请求。

Docker Compose 自动读取本机覆盖配置。配置或更换登录文件后执行 `docker compose up -d --build --force-recreate`。登录过期、账号没有查看权限时，需要更新本地文件；不要把 Cookie 内容填入页面输入框或提交到仓库。

### 解析速度

抖音和 X 的成功解析结果在内存中缓存 2 分钟，最多保留 128 组结果；重复分享参数会被归一化，相同内容的并发请求合并为一次平台调用。缓存只保存媒体元数据，每次 API 请求仍会生成新的下载入口。空结果和异常不缓存，下载遇到地址失效时会清除缓存，让重新解析真正获取新地址。

X 缓存按登录文件的实际内容隔离，更新登录文件后不复用旧账号结果。X 官方接口使用独立线程池，浏览器解析仍串行执行，避免超过容器进程限制。缓存主要加速重复链接；首次解析仍取决于平台接口和网络响应时间。

## 5. 安全与限制

- `compose.yaml` 只绑定 Mac 回环地址 `127.0.0.1`。**不要**直接改成 `0.0.0.0` 并公网暴露：解析器会访问用户给出的目标 URL，存在 SSRF / DNS 重绑定 / URL 跳转风险。
- Python 层拒绝显式本地/内网 IP 和大部分内部主机名，但不能全面防御 DNS 重绑定和 HTTP 重定向引起的内网访问。对外提供时要使用强鉴权、DNS/IP 每跳校验、出站网络策略、限流等安全控制。
- 站点解析逻辑可能失效、触发反爬、需要登录或 Cookie。Chromium 浏览器兜底在部分 Docker/宿主机安全环境可能不可用。
- 本版本只流式转发抖音和 X 下载，不保存视频；如后续增加服务器批量下载，应新增任务队列、文件存储、容量限制和清理策略。
- 上游使用 **PolyForm Noncommercial 1.0.0** 非商业许可证；本封装只供学习和个人非商业使用。须遵守版权和平台规则。
- 已验证镜像构建、健康接口、回归测试及抖音、X 示例视频的在线解析；其他平台的在线解析成功率尚未验证。
