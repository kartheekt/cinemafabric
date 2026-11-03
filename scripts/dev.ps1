<#
.SYNOPSIS
  Start the fabric and the studio, and optionally join this machine's devices and models.

.EXAMPLE
  ./scripts/dev.ps1                 # fabric + studio + this machine as a node
  ./scripts/dev.ps1 -Mode mock      # simulated workers, no GPUs or network needed
  ./scripts/dev.ps1 -NoNode         # fabric + studio only
#>
param(
    [ValidateSet("live", "mock")][string]$Mode = "live",
    [switch]$NoNode,
    [string]$BindHost = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not $env:UV_PROJECT_ENVIRONMENT) { $env:UV_PROJECT_ENVIRONMENT = Join-Path $HOME ".venvs\cinemafabric" }
$env:CF_MODE = $Mode

function Start-Window([string]$Title, [string]$Command) {
    $full = "`$Host.UI.RawUI.WindowTitle = '$Title'; Set-Location '$root'; $Command"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $full | Out-Null
    Write-Host "started $Title"
}

function Wait-Url([string]$Url) {
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-RestMethod -Uri $Url -TimeoutSec 2 | Out-Null; return } catch { Start-Sleep -Seconds 1 }
    }
}

Start-Window "fabric :8100" "uv run uvicorn cinemafabric.fabric.app:app --host $BindHost --port 8100"
Start-Window "studio :8000" "uv run uvicorn cinemafabric.studio.app:app --host $BindHost --port 8000"

if ($Mode -eq "live" -and -not $NoNode) {
    Wait-Url "http://127.0.0.1:8100/health"
    $tokenFile = Join-Path $root "data\join_token"
    if (-not $env:CF_JOIN_TOKEN) { $env:CF_JOIN_TOKEN = (Get-Content $tokenFile -Raw).Trim() }
    Start-Window "node" "uv run python -m cinemafabric.node --join http://127.0.0.1:8100"
}

if (-not (Test-Path (Join-Path $root "frontend\dist\index.html"))) {
    Write-Host "UI not built yet: npm --prefix frontend install; npm --prefix frontend run build"
}
Write-Host "Open http://127.0.0.1:8000"
