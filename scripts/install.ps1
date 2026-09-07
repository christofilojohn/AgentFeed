# AgentFeed installer for Windows (PowerShell).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Domain = if ($args[0]) { $args[0] } else { "generic" }

function Say($m) { Write-Host "`n$m" -ForegroundColor Cyan }

Say "1/4  Python environment"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv venv --python 3.13
uv pip install -q -e .

Say "2/4  Local model runtime"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  Write-Host "  ollama found"
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
} else {
  Write-Host "  ! Install Ollama from https://ollama.com/download" -ForegroundColor Yellow
}

Say "3/4  Models"
if (Get-Command ollama -ErrorAction SilentlyContinue) {
  # Ollama's default context is far too small for whole articles.
  [Environment]::SetEnvironmentVariable("OLLAMA_CONTEXT_LENGTH", "32768", "User")
  $model = if ($env:AGENTFEED_MODEL) { $env:AGENTFEED_MODEL } else { "qwen3:8b" }
  ollama pull $model
  ollama pull nomic-embed-text
}

Say "4/4  Database and domain pack"
.\.venv\Scripts\agentfeed.exe init --domain $Domain

Write-Host @"

Ready.

  .venv\Scripts\agentfeed add techcrunch
  .venv\Scripts\agentfeed update
  .venv\Scripts\agentfeed serve

  Dashboard  http://127.0.0.1:8770/
  Discovery  http://127.0.0.1:8770/.well-known/agent-feed
"@ -ForegroundColor Green
