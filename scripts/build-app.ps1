# Build AgentFeed.exe (run on Windows). Output: dist\AgentFeed\AgentFeed.exe
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
uv pip install -q -e ".[desktop]"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
.\.venv\Scripts\pyinstaller.exe --noconfirm --clean packaging\agentfeed.spec
Write-Host "`nBuilt: dist\AgentFeed\AgentFeed.exe"
Write-Host "Ship:  Compress-Archive dist\AgentFeed dist\AgentFeed-windows.zip"
