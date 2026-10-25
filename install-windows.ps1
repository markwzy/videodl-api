# 使用固定的解释器版本创建独立环境，避免服务器上其他应用的依赖互相覆盖。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Get-Command python -ErrorAction Stop | Out-Null
& python -c "import struct, sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"
if ($LASTEXITCODE -ne 0) { throw "请使用 64 位 Python 3.12，并将它加入当前账户的 PATH。" }
& python -m venv "$PSScriptRoot\.venv"
if ($LASTEXITCODE -ne 0) { throw "创建环境失败，请先安装 64 位 Python 3.12。" }

$Python = "$PSScriptRoot\.venv\Scripts\python.exe"
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "更新安装工具失败，请检查网络。" }
& $Python -m pip install -r "$PSScriptRoot\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "安装依赖失败，请查看上方错误信息。" }

Write-Host "依赖安装完成。确认已安装 Chrome 和 FFmpeg 后，运行 .\start-windows.ps1。"
