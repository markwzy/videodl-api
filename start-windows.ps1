param(
    [string]$ListenAddress = "127.0.0.1",
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [string]$BrowserPath = $env:BROWSER_PATH,
    [string]$XCookiesFile = $env:X_COOKIES_FILE
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

# 默认文件相对于项目目录解析，计划任务和其他工作目录启动时也使用同一份配置。
# 显式指定的路径配错应报错，不能静默切换到匿名访问或另一个账号。
if ([string]::IsNullOrWhiteSpace($XCookiesFile)) {
    $XCookiesFile = Join-Path $PSScriptRoot ".secrets\x-cookies.txt"
    if (-not (Test-Path -LiteralPath $XCookiesFile)) { $XCookiesFile = "" }
}
if (-not [string]::IsNullOrWhiteSpace($XCookiesFile)) {
    if (-not (Test-Path -LiteralPath $XCookiesFile -PathType Leaf)) {
        throw "X_COOKIES_FILE 必须指向存在的 Cookie 文件，请检查 -XCookiesFile 或环境变量。"
    }
    $XCookiesFile = (Resolve-Path -LiteralPath $XCookiesFile).Path
    $CookieStream = $null
    try {
        $CookieStream = [System.IO.File]::OpenRead($XCookiesFile)
    } catch {
        throw "无法读取 X_COOKIES_FILE，请检查运行账户的文件读取权限。"
    } finally {
        if ($null -ne $CookieStream) { $CookieStream.Dispose() }
    }
    $env:X_COOKIES_FILE = $XCookiesFile
    Write-Host "X：已配置登录文件，会话是否有效将在解析时验证。"
} else {
    Remove-Item Env:X_COOKIES_FILE -ErrorAction SilentlyContinue
    Write-Host "X：匿名模式，未配置登录文件；可将文件放到 .secrets\x-cookies.txt。"
}

# 队列、缓存及下载凭证属于单个进程，不能增加工作进程数量或启用自动重载。
& $Python -m uvicorn app.main:app --host $ListenAddress --port $Port --workers 1
exit $LASTEXITCODE
