param(
    [string]$ListenAddress = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BrowserPath = $env:BROWSER_PATH
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Scripts = "$PSScriptRoot\.venv\Scripts"
$Python = "$Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "缺少虚拟环境，请先运行 .\install-windows.ps1。"
}

# 提取器通过命令名查找运行时，显式使用虚拟环境附带的版本，无需修改系统配置。
$env:PATH = "$Scripts;$env:PATH"
$env:PYTHONUNBUFFERED = "1"
Get-Command ffmpeg -ErrorAction Stop | Out-Null
Get-Command ffprobe -ErrorAction Stop | Out-Null
Get-Command node -ErrorAction Stop | Out-Null
if ($BrowserPath) {
    if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) {
        throw "浏览器路径不存在，请指定已安装的 Chrome 或 Edge 可执行文件。"
    }
    $env:BROWSER_PATH = (Resolve-Path -LiteralPath $BrowserPath).Path
}

# 队列、缓存及下载凭证属于单个进程，不能增加工作进程数量或启用自动重载。
& $Python -m uvicorn app.main:app --host $ListenAddress --port $Port --workers 1
exit $LASTEXITCODE
