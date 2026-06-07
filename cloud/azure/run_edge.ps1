# Deploy + run the posture-api module on THIS machine (the dorm edge node) using
# the IoT Edge simulator (iotedgehubdev). Windows 11 Home has no Hyper-V, so we
# use the simulator (runs in Docker Desktop) instead of EFLOW.
#
# Prereq: Docker Desktop running; setup_azure.ps1 + build_and_push.ps1 done.
# NOTE: `iotedgehubdev setup` may need an elevated (Administrator) PowerShell.
#
#   .\cloud\azure\run_edge.ps1            # setup + start the module
#   .\cloud\azure\run_edge.ps1 -SetModules # also push manifest to IoT Hub (portal proof)
param(
    [switch]$SetModules
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $here "..\..")

# --- load .env --------------------------------------------------------------
$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) { throw "cloud/azure/.env not found — run setup_azure.ps1 first." }
$vars = @{}
foreach ($l in Get-Content $envFile) {
    $t = $l.Trim(); if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $kv = $t -split "=", 2; if ($kv.Count -eq 2) { $vars[$kv[0].Trim()] = $kv[1].Trim() }
}
$conn = $vars["EDGE_CONNECTION_STRING"]
if (-not $conn) { throw ".env missing EDGE_CONNECTION_STRING" }

# --- render deployment.generated.json --------------------------------------
& (Join-Path $here "gen_deployment.ps1")
$deployment = Join-Path $here "deployment.generated.json"

# --- ensure iotedgehubdev (prefer the repo .venv) --------------------------
$venvPy  = Join-Path $repoRoot ".venv\Scripts\python.exe"
$py      = if (Test-Path $venvPy) { $venvPy } else { "python" }
$iehd    = Join-Path (Split-Path $py) "iotedgehubdev.exe"
if (-not (Test-Path $iehd)) {
    Write-Host "Installing iotedgehubdev ..." -ForegroundColor Cyan
    & $py -m pip install --quiet iotedgehubdev
}
if (-not (Test-Path $iehd)) { $iehd = "iotedgehubdev" }  # fall back to PATH

# --- simulator setup + start -----------------------------------------------
Write-Host "iotedgehubdev setup ..." -ForegroundColor Green
& $iehd setup -c $conn

Write-Host "iotedgehubdev start -d (postureApi module) ..." -ForegroundColor Green
& $iehd start -d $deployment

Write-Host "`nModule running. Local check:" -ForegroundColor Cyan
Write-Host "  docker ps                 # expect postureApi + edgeHub"
Write-Host "  curl http://localhost:8000/health"
Write-Host "  .\.venv\Scripts\python.exe scripts\verify_live.py"

# --- optional: register the deployment on IoT Hub (Azure portal evidence) ---
if ($SetModules) {
    $hub = $vars["IOT_HUB_NAME"]; $dev = $vars["EDGE_DEVICE_ID"]
    Write-Host "`naz iot edge set-modules -> $hub / $dev ..." -ForegroundColor Green
    az iot edge set-modules --hub-name $hub --device-id $dev --content $deployment --only-show-errors | Out-Null
    Write-Host "Portal: IoT Hub > Devices > $dev > Modules should now list 'postureApi'." -ForegroundColor Cyan
}
