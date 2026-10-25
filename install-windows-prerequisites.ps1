#Requires -Version 5.1
#Requires -RunAsAdministrator

# 安装到独立目录并配置机器环境变量，计划任务使用其他账户运行时也能找到依赖。
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "请在 64 位 Windows 的管理员 PowerShell 中运行此脚本。"
}

$PythonDir = Join-Path $env:ProgramFiles "Python312"
$PythonExe = Join-Path $PythonDir "python.exe"
$ChromeExe = Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
$FfmpegDir = Join-Path $env:ProgramFiles "VideoDL\FFmpeg"
$FfmpegBin = Join-Path $FfmpegDir "bin"
$DownloadDir = Join-Path ([IO.Path]::GetTempPath()) ("videodl-prerequisites-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $DownloadDir | Out-Null

function Assert-InstallerSignature([string]$Path, [string]$Publisher) {
    $Signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notlike "*$Publisher*") {
        throw "安装包签名验证失败：$Path。请检查网络和服务器证书，不要运行该文件。"
    }
}

try {
    Write-Host "[1/4] 安装 64 位 Python 3.12..."
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        # 该版本是官方提供传统 Windows 安装器的最后一个 3.12 维护版本。
        $Installer = Join-Path $DownloadDir "python.exe"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $Installer -TimeoutSec 1800
        Assert-InstallerSignature $Installer "Python Software Foundation"
        $Result = Start-Process -FilePath $Installer -ArgumentList @(
            "/quiet", "/norestart", "InstallAllUsers=1", "TargetDir=`"$PythonDir`"", "PrependPath=1",
            "Include_pip=1", "Include_test=0", "Include_launcher=1", "InstallLauncherAllUsers=1"
        ) -Wait -PassThru
        if ($Result.ExitCode -notin @(0, 3010)) { throw "Python 安装失败，退出码：$($Result.ExitCode)" }
        if ($Result.ExitCode -eq 3010) { Write-Warning "Python 安装提示需要重启服务器。" }
    }
    & $PythonExe -c "import struct, sys; assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8; print(sys.version)"
    if ($LASTEXITCODE -ne 0) { throw "Python 版本检查失败。" }

    Write-Host "[2/4] 安装 64 位 Chrome..."
    if (-not (Test-Path -LiteralPath $ChromeExe -PathType Leaf)) {
        $Installer = Join-Path $DownloadDir "chrome.msi"
        Invoke-WebRequest -UseBasicParsing -Uri "https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi" -OutFile $Installer -TimeoutSec 1800
        Assert-InstallerSignature $Installer "Google"
        $Result = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" -ArgumentList @(
            "/i", "`"$Installer`"", "/qn", "/norestart"
        ) -Wait -PassThru
        if ($Result.ExitCode -notin @(0, 3010)) { throw "Chrome 安装失败，退出码：$($Result.ExitCode)" }
        if ($Result.ExitCode -eq 3010) { Write-Warning "Chrome 安装提示需要重启服务器。" }
    }
    if (-not (Test-Path -LiteralPath $ChromeExe -PathType Leaf)) { throw "没有找到安装后的 Chrome。" }

    Write-Host "[3/4] 安装 FFmpeg 和 FFprobe..."
    if (-not ((Test-Path "$FfmpegBin\ffmpeg.exe") -and (Test-Path "$FfmpegBin\ffprobe.exe"))) {
        $Archive = Join-Path $DownloadDir "ffmpeg.zip"
        $Checksum = Join-Path $DownloadDir "ffmpeg.sha256"
        $Url = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Archive -TimeoutSec 1800
        Invoke-WebRequest -UseBasicParsing -Uri "$Url.sha256" -OutFile $Checksum -TimeoutSec 120
        $Expected = ((Get-Content -LiteralPath $Checksum -Raw).Trim() -split '\s+')[0]
        if ($Expected -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash -ne $Expected) {
            throw "FFmpeg 下载文件校验失败，请重新运行脚本。"
        }
        $Unpacked = Join-Path $DownloadDir "unpacked"
        Expand-Archive -LiteralPath $Archive -DestinationPath $Unpacked
        $Package = Join-Path $Unpacked "ffmpeg-9.0.2-essentials_build"
        if (-not (Test-Path "$Package\bin\ffmpeg.exe")) { throw "FFmpeg 压缩包目录结构异常。" }
        New-Item -ItemType Directory -Path $FfmpegDir -Force | Out-Null
        Copy-Item -Path "$Package\*" -Destination $FfmpegDir -Recurse -Force
    }
    & "$FfmpegBin\ffmpeg.exe" -version
    if ($LASTEXITCODE -ne 0) { throw "FFmpeg 启动检查失败。" }
    & "$FfmpegBin\ffprobe.exe" -version
    if ($LASTEXITCODE -ne 0) { throw "FFprobe 启动检查失败。" }

    Write-Host "[4/4] 配置系统环境变量..."
    # 保留已有路径，只将本项目需要的目录放到最前，重复运行不会持续追加相同条目。
    $Paths = @($PythonDir, "$PythonDir\Scripts", $FfmpegBin)
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $Existing = @($MachinePath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -notin $Paths })
    [Environment]::SetEnvironmentVariable("Path", (($Paths + $Existing) -join ';'), "Machine")
    $env:Path = ($Paths -join ';') + ";" + $env:Path

    Write-Host "安装完成。请关闭并重新打开 PowerShell，再到项目目录执行："
    Write-Host "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1"
    Write-Host "powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start-windows.ps1"
} finally {
    # 仅删除本次创建的临时目录，失败时也不清理其他安装或用户文件。
    Remove-Item -LiteralPath $DownloadDir -Recurse -Force -ErrorAction SilentlyContinue
}
