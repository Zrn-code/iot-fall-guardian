# Build the posture-api IoT Edge module image and push it to ACR.
# Reuses the EXISTING posture-api/Dockerfile unchanged — the module IS the
# inference container. Run from the repo root (or anywhere; paths are resolved).
#
#   .\cloud\azure\build_and_push.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $here "..\..")

# Load .env for ACR name + image tag.
$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) { throw "cloud/azure/.env not found — run setup_azure.ps1 first." }
$vars = @{}
foreach ($l in Get-Content $envFile) {
    $t = $l.Trim(); if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $kv = $t -split "=", 2; if ($kv.Count -eq 2) { $vars[$kv[0].Trim()] = $kv[1].Trim() }
}
$acrName = $vars["ACR_NAME"]
$image   = $vars["MODULE_IMAGE"]
if (-not $acrName -or -not $image) { throw ".env missing ACR_NAME / MODULE_IMAGE" }

Write-Host "Building $image from posture-api/Dockerfile ..." -ForegroundColor Green
docker build -f (Join-Path $repoRoot "posture-api\Dockerfile") -t $image (Join-Path $repoRoot "posture-api")

Write-Host "az acr login -n $acrName ..." -ForegroundColor Cyan
az acr login -n $acrName

Write-Host "Pushing $image ..." -ForegroundColor Green
docker push $image

Write-Host "`nDone -> $image" -ForegroundColor Cyan
Write-Host "Next: .\cloud\azure\gen_deployment.ps1" -ForegroundColor Cyan
