# VideoDL API (Mac + Docker)

基于 [CharlesPikachu/videodl](https://github.com/CharlesPikachu/videodl) / `videofetch==0.9.1`，并使用 `yt-dlp[default]==2026.10.21` 解析 X / Twitter、YouTube 和小红书。可以直接粘贴带文案的分享链接，返回视频标题、封面、媒体 URL、音频 URL 等。抖音、小红书、X 和 YouTube 支持通过本地服务流式下载到浏览器，**服务器不保存视频文件**。

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

### iPhone 快捷指令与 GET 接口

`GET /api/parse?text=<经过网址编码的链接或整段分享文案>` 等待解析完成后返回 HTTP 200 和解析结果，无需查询任务。与页面共用平台识别、账号隔离、有界队列、解析时限和下载凭证。

```bash
# 返回 JSON；使用 --data-urlencode 保留中文、加号和分享链接中的签名参数。
curl -G 'http://localhost:8000/api/parse' \
  --data-urlencode 'text=分享视频：https://v.douyin.com/abc123/'

# 浏览器打开这个入口时，解析完成后自动跳转下载。
curl -G 'http://localhost:8000/api/parse' \
  --data-urlencode 'text=https://v.douyin.com/abc123/' \
  --data-urlencode 'redirect=true' -D - -o /dev/null
```

默认 JSON 包含下文成功任务 `result` 中的全部字段，另有顶层 `browser_download_url`，例如 `http://你的服务地址:8000/api/download/<短期凭证>`。这是可直接交给“打开 URL”的完整下载地址，域名和协议来自本次请求；通过反向代理访问时，应正确转发外部域名、协议和路径前缀。`items[].local_download_url` 仍为本站相对路径。

加上 `redirect=true`，成功时返回 HTTP 303，跳转至**第一条视频、最高可用画质**的本站下载入口。抖音、小红书、X 和 YouTube 当前支持这条下载链路；其他平台仍可返回解析 JSON，但 `browser_download_url` 为 `null`，请求跳转时返回 422，不会直接跳转到可能缺少请求头的原始媒体地址。下载凭证有效期为 10 分钟，服务器不保存视频。

快捷指令可以按以下步骤配置：

1. 新建“下载分享视频”，开启“在共享表单中显示”，接收“文本”和“URL”。从其他 App 分享给快捷指令的设置可参考 [Apple 官方说明](https://support.apple.com/zh-cn/guide/shortcuts/apd163eb9f95/ios)。
2. 取“快捷指令输入”；如果没有输入，使用“获取剪贴板”。这样既能从分享菜单运行，也能复制抖音等平台的分享文案后手动运行。
3. 对输入执行“URL 编码”。**只编码分享内容这一个参数值，编码一次**，不要编码整个接口地址，否则签名参数中的 `&`、`+`、`#` 等可能被误解。
4. 添加“文本”，内容为 `http://你的服务地址:8000/api/parse?text=【URL 编码的结果】&redirect=true`，将方括号部分替换为前一步的变量。
5. 添加“打开 URL”，传入这个文本。浏览器等待解析并进入下载流程；是否出现下载确认由浏览器设置决定，文件通过浏览器下载管理保存，不会自动进入照片图库。

如果希望先取得 JSON，再打开下载地址，第 4 步不加 `redirect=true`，改用“获取 URL 内容”（GET），从响应中取 `browser_download_url`，非空时再“打开 URL”。这类操作可参考 [Apple 网络 API 使用说明](https://support.apple.com/zh-cn/guide/shortcuts/apd2d448b2de/ios)。不要用“获取 URL 内容”调用跳转模式，否则快捷指令可能直接接收整个视频，而不是交给浏览器下载。

首次解析可能较慢，默认排队最多 30 秒、执行最多 90 秒；客户端或反向代理可能先超时，较慢平台也可沿用下文 POST 提交和任务查询。GET 失败返回 JSON 错误：输入无效或不支持直接下载为 422，队列满为 503（含 `Retry-After`），等待或解析超时为 504，解析器错误通常为 502。连接断开不会取消其他客户端共享的解析。GET 查询内容会进入浏览器历史和可能的访问日志，不要在文案中放入账号 Cookie。

**iPhone 访问地址：**当前 Compose 仅绑定 Mac 的 `127.0.0.1`，手机上的 `localhost` 指向手机自身。若已通过受保护的服务地址访问页面，快捷指令使用同一地址即可。仅在可信家庭局域网内使用时，可将 `compose.yaml` 中的端口绑定改为 Mac 的具体局域网地址，例如 `"192.168.1.20:8000:8000"`（替换成实际地址），重新执行 `docker compose up -d`，并让手机与 Mac 连接同一网络。先在手机浏览器打开 `http://192.168.1.20:8000/api/health` 确认连通，再使用该地址配置快捷指令；不要为此开放路由器公网端口。默认绑定不会自动更改。

### POST 提交与任务查询

```bash
curl -X POST 'http://localhost:8000/api/parse' \
  -H 'Content-Type: application/json' \
  -d '{"text":"分享视频：https://www.bilibili.com/video/BV..."}'
```

请求体：`{"text":"<视频链接或者整段分享文案>"}`，可附加 `request_id`（20～64 位随机字母、数字、下划线或短横线），同一次提交因断网重试时复用该标识。接口立即返回 **HTTP 202** 和任务信息，不再等待解析完成：

```json
{"id":"随机任务标识","status":"queued","input_url":"https://www.bilibili.com/video/BV...","queue_position":1,"created_at":1792790000,"started_at":null,"finished_at":null,"result":null,"error":null}
```

随后每隔约 1 秒调用 `GET /api/tasks/{id}` 查询。状态为 `queued`（排队）、`running`（解析中）、`succeeded`（成功）或 `failed`（失败）；`queue_position` 只在排队时有值。成功时 `result` 包含以下媒体字段，失败时 `error` 包含 `status_code` 和 `detail`。例如：

```bash
curl 'http://localhost:8000/api/tasks/<提交接口返回的任务标识>'
```

任务标识也是查询凭证，应按下载链接一样保管。不存在、已过期或因容量限制移除的任务返回 HTTP 404。任务状态只保存在当前服务进程内，服务重启后失效。

成功任务的 `result` 字段：

- `success`：是否拿到有效媒体地址
- `input_url`：提取的原始 URL
- `count`：有效结果数（最多 20 条）
- `items[]`：每项包含 `title`、`source`、`identifier`、`cover_url`、`download_url`、`audio_download_url`、`ext`、`is_hls`、`requires_merge`、`may_require_headers`
- `message`：解析状态说明
- `items[].local_download_url`：抖音、小红书、X 和 YouTube 视频的本地下载入口，页面显示为“下载视频”。凭证 10 分钟有效，重启服务或过期后需重新解析。
- `items[].formats[]`：抖音、小红书、X 和 YouTube 实际可用的清晰度，按分辨率、帧率和码率从高到低排列，同尺寸保留一个版本；包含 `format_id`、`label`、`width`、`height`、`fps`、`bitrate`（每秒比特数）、`filesize`（字节）、`codec`、`ext`、`download_url`、`local_download_url` 和 `requires_merge`（下载时是否需要合成音轨）。平台未提供的尺寸、帧率或大小为 `null`。顶层下载地址默认对应第一档。

部分 `download_url` 是限时 URL、m3u8，或者需要携带视频站点请求头/Cookie。解析出 URL **不等于**浏览器可以直接保存 MP4。

抖音通过容器内的 Chromium 打开视频页，读取网页正常发出的作品详情响应，并校验作品 ID 后返回播放地址。支持单条视频链接和短分享链接，不扫描页面中的宣传视频。当前不传入登录 Cookie；需要登录、验证或已经失效的作品可能无法解析。未取得有效媒体地址时，任务进入 `failed`，`error.status_code` 为 502 并附带失败原因。

X / Twitter 使用专用提取器访问官方接口，支持 `x.com`、`twitter.com` 的推文链接和一条推文中的多个视频，选择包含音画的 HTTPS MP4。需要登录的推文必须配置有访问权限的账号登录文件，不会交给第三方解析站点或通用页面扫描。

小红书复用现有 `yt-dlp` 的专用提取器，支持 `/explore/`、`/discovery/item/` 笔记链接及 `xhslink.com` 短分享链接。完整保留分享签名，只读取目标笔记的视频数据，不扫描页面推荐素材；图片笔记、无权限或失效链接会明确报错。视频地址统一使用 HTTPS，并通过本站下载入口保留所需请求头，支持画质选择、预览和范围下载。

YouTube 使用专用提取器，只解析链接指定的视频，不扫描广告或推荐素材。支持短分享链接、完整观看页和 Shorts；播放列表参数不会触发批量解析。当前匿名访问，需登录、地区限制或验证的视频会明确报错，直播暂不支持。复用上游依赖附带的 Node.js 24，镜像构建时安装配套解码脚本，解析时不临时下载远程脚本。

YouTube 常将高清画面和音轨分开提供；点击“下载视频”后，由 FFmpeg 不转码合成为带声音的流式 MP4。两个输入均通过原有下载域名校验，FFmpeg 仅能读取管道，不能自行联网；服务器不保存媒体。合成任务最多同时运行 2 个，满额返回 503。合成输出大小未知，不支持断点续传和本站预览，中断后需重新下载；编码能否播放取决于本地播放器。

原始媒体地址在浏览器里直接打开可能返回 403。请点击“下载视频”，由本地服务保留媒体访问所需的请求头，边读取边发送到浏览器；单文件支持单段范围请求和断点续传，不会先下载完整文件或将其装入内存。下载入口只接受本次解析生成的凭证，每次重定向也限定在原平台的媒体域名内。

页面支持一键粘贴、平台识别、`Ctrl / ⌘ + Enter` 解析、画质卡片、视频预览和本页解析历史，并适配手机与系统深浅色。粘贴后可继续编辑文案，需点击“开始解析”或使用提交快捷键才会发起解析。手机底部快捷下载条跟随当前选中的视频与画质，入口过期后改为重新解析。历史只在当前页面内存中保留最近 8 个来源链接和标题，点击“重新解析”获取新的下载入口。当前任务的来源和随机提交标识保存在当前标签页的 `sessionStorage`，刷新后自动恢复排队、解析进度或尚未过期的结果；浏览器禁用存储时无法恢复。清空输入会停止查询并忘记当前任务，不会取消其他页面可能共享的服务端解析。

抖音、小红书、X 和 YouTube 的画质卡片默认选择最高可用版本。切换后下载按钮、预览、复制下载入口和原始媒体地址同步切换，不需要再次解析；多条视频独立选择画质。“默认最高画质”关闭后选择最低可用版本，“显示独立音频”只控制已有音频地址的展示，YouTube 下载不受该开关影响。只显示平台实际提供的 MP4 版本，不生成或放大分辨率；竖屏按短边标注，例如 `1080×1920` 显示为 `1080p`。平台未提供的编码、尺寸、文件大小显示为未知；其他平台当前仍使用各自解析器的默认地址。

“复制下载入口”复制本站短期凭证地址，由服务端携带请求头，需在能访问本站的浏览器中打开。原始媒体地址单独复制不会携带请求头，仍可能返回 403。倒计时从请求开始保守估算，仅表示本站凭证有效期，不能代表原平台直链有效期；凭证也可能因服务重启或容量限制提前失效。预览使用同一下载入口，浏览器不支持编码时请下载后本地播放。

页面保留 12 个广告位的原始插入位置、`data-ad-slot` 标识和样式，目前广告容器、工具推广及手机悬浮广告交互均已注释，不显示占位内容或占用空白空间。以后接入广告时，搜索对应点位标识，替换占位素材并取消该块注释；左侧广告栏恢复后桌面自动切回三栏。手机悬浮广告需同时恢复“手机悬浮广告预留交互”脚本，启用后仍与快捷下载条互斥。

## 3. 停止、启动和查看日志

```bash
docker compose stop         # 暂停容器
docker compose start        # 重启容器
docker compose logs -f      # 日志
docker compose down         # 停止并删除容器（保留镜像）
```

## 4. 可选配置

默认 `VIDEO_SOURCES` 启用 11 个平台，其中 `DouyinVideoClient` 启用抖音浏览器解析，`TwitterVideoClient` 启用 X 专用提取器，`YouTubeVideoClient` 启用 YouTube 专用提取器，`RednoteVideoClient` 启用小红书专用提取器，其余平台使用上游专用解析器和页面抓取。需要其他平台可编辑 `compose.yaml` 中的 `VIDEO_SOURCES`，使用上游注册的准确解析器名称。

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

抖音、小红书、X 和 YouTube 的成功解析结果在内存中缓存 2 分钟，最多保留 128 组结果；抖音、X 和 YouTube 的重复分享参数会被归一化，小红书保留完整签名参数，不合并不同签名的链接。相同链接的并发请求合并为一次平台调用。缓存只保存媒体元数据，每个新解析任务仍会生成新的下载入口；查询或恢复同一个任务复用原来的入口，刷新不会延长凭证有效期。空结果和异常不缓存，下载遇到地址失效时会清除缓存，让重新解析真正获取新地址。

X 缓存和正在执行的任务按登录文件的实际内容隔离，更新登录文件后不复用旧账号结果。X、YouTube 和小红书的专用提取器与浏览器解析独立排队，浏览器解析仍串行执行，避免超过容器进程限制。缓存主要加速重复链接；首次解析仍取决于平台接口和网络响应时间。

### 排队与刷新恢复

- 浏览器类平台同时执行 1 个任务，X、YouTube 和小红书共用接口队列，同时执行 2 个任务；每条队列默认最多另有 8 个等待任务，由 `PARSE_QUEUE_SIZE` 控制。
- 队列满时提交返回 HTTP 503 和 `Retry-After: 5`，不会再往线程池无限提交。正在排队或执行的相同链接复用原任务，不占额外名额。
- 排队默认超过 30 秒结束，任务错误为 504；通过 `PARSE_QUEUE_TIMEOUT` 调整。等待超时的任务不会访问视频平台。
- `PARSE_TIMEOUT` 默认 90 秒，从实际开始执行计算，不包含排队时间。超时会将任务标为失败，但 Python 线程仍可能继续运行，因此直到它真正退出才释放执行名额。当前没有强制终止工作进程的能力。
- 已结束的任务在内存中保留 2 分钟，最多保留 128 个，超量移除最早结束的任务；因此旧任务可能提前失效。服务重启后需要重新提交。
- 保持 Dockerfile 中的 `--workers 1`。队列、任务状态和下载凭证都属于当前进程，不能直接通过增加进程数扩容。

抖音首次需要访问网页时启动 Chromium，之后复用这个进程，每次解析都创建独立的临时浏览环境。完成、超时或异常后销毁整个环境，清除页面、弹窗、Cookie 和存储；保留空白浏览器供下一次使用，所以解析不同链接或缓存过期后也能省去重复启动和退出的时间。浏览器退出或清理失败后，下次请求会重新启动。服务停止或切换到其他平台解析时释放常驻浏览器，避免多个浏览器叠加超过容器资源限制。

## 5. 安全与限制

- `compose.yaml` 只绑定 Mac 回环地址 `127.0.0.1`。**不要**直接改成 `0.0.0.0` 并公网暴露：解析器会访问用户给出的目标 URL，存在 SSRF / DNS 重绑定 / URL 跳转风险。
- Python 层拒绝显式本地/内网 IP 和大部分内部主机名，但不能全面防御 DNS 重绑定和 HTTP 重定向引起的内网访问。对外提供时要使用强鉴权、DNS/IP 每跳校验、出站网络策略、限流等安全控制。
- 站点解析逻辑可能失效、触发反爬、需要登录或 Cookie。Chromium 浏览器兜底在部分 Docker/宿主机安全环境可能不可用。
- 本版本只流式转发抖音、小红书、X 和 YouTube 下载，不保存视频；如后续增加服务器批量下载，应新增任务队列、文件存储、容量限制和清理策略。
- 上游使用 **PolyForm Noncommercial 1.0.0** 非商业许可证；本封装只供学习和个人非商业使用。须遵守版权和平台规则。
- 已验证镜像构建、健康接口、回归测试及抖音、小红书、X 和 YouTube 示例视频的在线解析；其余平台的在线解析成功率尚未验证。

## 6. 软件许可证

本项目自有代码采用 **[PolyForm Noncommercial License 1.0.0](LICENSE)**，版权及上游声明见 [NOTICE](NOTICE)。

- 允许个人学习、研究、测试、兴趣项目等非商业用途，并允许在许可范围内修改和分发。
- 不授予商业用途许可，例如售卖本软件、提供商业收费解析或下载服务，或集成到商业产品中。
- 分发原版或修改版时，必须保留许可证和要求保留的版权声明；第三方组件仍适用其各自的许可证。
- 软件按现状提供，不提供担保。具体授权范围以 [LICENSE](LICENSE) 正文为准。

这是源码可用的非商业许可证，不属于 [OSI 定义的开源许可证](https://opensource.org/osd)，因为它限制商业用途。
