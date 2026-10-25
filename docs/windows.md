# Windows 原生部署

面向 Windows Server 2019 / 2022 / 2025 的 64 位系统，直接运行 Python，不需要 Docker、WSL 或虚拟机。安装账户与运行账户应保持一致；建议使用普通专用账户运行。

## 1. 安装系统依赖

1. 安装 [Python 3.12（64 位）](https://www.python.org/downloads/windows/)，勾选安装器中的“Add python.exe to PATH”。打开新的 PowerShell，确认 `python --version` 可以运行。
2. 安装 [Google Chrome](https://www.google.com/chrome/)。项目复用现有 DrissionPage 的系统查找能力；使用 Edge 或自定义安装位置时，启动时指定浏览器的完整路径。
3. 从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html#build-windows) 选择 Windows 构建，解压后将包含 `ffmpeg.exe` 和 `ffprobe.exe` 的 `bin` 目录加入运行账户的 `PATH`。打开新的 PowerShell，确认以下命令均能运行：

```powershell
python --version
ffmpeg -version
ffprobe -version
```

Node.js 24 随 `videofetch` 的现有依赖安装到虚拟环境，启动脚本会将该目录放在当前进程的 `PATH` 最前面，无需另外安装 Node.js。Windows 管道使用 `pywin32`，仅在 Windows 上安装。

服务器需要能连接 PyPI、视频站点及其媒体域名。解析器的站点限制、登录要求与 Docker 版本相同。

## 2. 上传项目并安装依赖

将当前分支的项目文件复制到服务器，例如 `C:\videodl-api`。至少包含 `app` 目录（含静态页面）、`requirements.txt` 和两个 PowerShell 脚本。不要复制其他电脑上的 `.venv`。

在 PowerShell 中执行：

```powershell
cd C:\videodl-api
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

脚本创建 `.venv` 并安装项目依赖；命令中的执行策略仅对本次进程生效，不修改机器策略。受组织组策略约束的服务器，需按管理员规定允许运行脚本。

## 3. 启动与检查

```powershell
cd C:\videodl-api
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1
```

默认查找 Chrome。如使用 Edge：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1 -BrowserPath "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

自定义端口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1 -Port 8080
```

服务默认监听 `127.0.0.1:8000`。在服务器本机浏览器打开：

- 页面：<http://localhost:8000>
- 接口文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/api/health>

也可以在另一个 PowerShell 中检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

健康检查只确认接口可用；再提交一条公开抖音链接验证浏览器，提交公开 YouTube 链接并下载分离音轨的画质验证媒体合成。

服务在前台输出日志，按 `Ctrl+C` 停止。重启后队列、缓存、任务和下载凭证均失效，需重新解析。不要增加工作进程数，不要同时启动多个实例监听同一地址和端口。

## 4. 可选环境变量

环境变量在启动前设置；`compose.yaml` 与 `compose.override.yaml` 在原生运行时不会被读取。

启动脚本会自动加载项目目录中的 `.secrets\x-cookies.txt`，无需额外设置。启动时显示“已配置登录文件”表示文件存在、可读，并已传给服务；不会检查或打印 Cookie 内容，也不代表平台仍认可该会话。文件不存在且未显式指定路径时显示“匿名模式”。

使用其他文件时，可以通过参数显式指定：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1 -XCookiesFile "D:\private\x-cookies.txt"
```

优先使用 `-XCookiesFile` 参数，其次使用 `X_COOKIES_FILE` 环境变量；两者均未指定时才查找项目默认文件。显式指定的文件不存在或不可读时停止启动，不会静默匿名访问。相对路径按项目目录解析。修改配置后需重启原来运行的服务，不能只在另一个终端设置变量。

```powershell
$env:PARSE_TIMEOUT = "90"
$env:PARSE_QUEUE_SIZE = "8"
$env:PARSE_QUEUE_TIMEOUT = "30"
$env:X_COOKIES_FILE = "C:\videodl-api\.secrets\x-cookies.txt"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1
```

未提供任何登录文件时匿名访问 X。登录文件必须为 Netscape 格式，并使用 Windows 文件权限限制为运行账户可读；不要提交到仓库。解析器使用独立临时副本，不修改原件。

`BROWSER_PATH` 也可以通过环境变量指定。路径必须是实际的浏览器可执行文件，配错会报错，不会静默换用其他浏览器。原生部署保留浏览器沙箱，不设置 `BROWSER_NO_SANDBOX`；该开关由 Docker 镜像单独启用。

## 5. 开机启动

使用 Windows 自带的“任务计划程序”创建任务：

- 运行账户使用上述安装、测试成功的专用账户，选择“不管用户是否登录都要运行”，不需要最高权限。
- 触发器选择“启动时”，可延迟 30 秒等待网络就绪。
- 程序填写 `powershell.exe`。
- 参数填写 `-NoProfile -ExecutionPolicy Bypass -File "C:\videodl-api\start-windows.ps1"`，需要自定义浏览器时追加 `-BrowserPath` 参数。
- 起始于填写 `C:\videodl-api`。
- 关闭运行时长上限；设置失败后每隔 1 分钟重试，并选择已有实例运行时“不启动新实例”。

创建后先手动运行任务，再检查健康接口；停止服务使用任务计划程序的“结束”。强制结束可能无法执行浏览器清理，计划维护时优先使用前台 `Ctrl+C` 正常退出。

默认端口只供服务器本机访问。如需远程使用，应接入有鉴权的反向代理并限制访问范围；当前服务没有登录鉴权，解析请求也没有全面阻断 DNS 重绑定及重定向内网访问。`-ListenAddress` 可以调整绑定地址，但不能把裸服务直接暴露到公网。

## 6. 实现与验证范围

Windows 合成以标准输入接收视频，以随机名称、仅本机可连接的命名管道接收音轨；FFmpeg 不接收远程媒体地址。音轨只允许按 MP4 容器读取，不解释外部播放列表。服务持续转发媒体，不创建视频或音频临时文件。正常结束先等待管道尾部被读完，下载中断则终止 FFmpeg、取消挂起的管道操作并释放并发名额。

仓库的 `.github/workflows/windows.yml` 在 Windows Server 2022 上安装原生依赖，运行全量离线回归、真实浏览器隔离环境测试、真实 FFmpeg 音画合成测试，以及启动脚本健康检查。工作流需要推送分支后运行；本地非 Windows 环境通过的测试不能代替该工作流或目标服务器上的验证。
