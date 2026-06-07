# Create the Azure resources for the IoT Edge demo (idempotent) and write
# cloud/azure/.env with the values the other scripts need.
#
# Prereq:  Azure CLI installed + `az login`.  (IoT extension is auto-added.)
# ⚠️ IoT Hub + ACR names are GLOBALLY UNIQUE — change -AcrName / -HubName if taken.
# ⚠️ Only ONE free F1 IoT Hub is allowed per subscription.
#
#   .\cloud\azure\setup_azure.ps1
param(
    [string]$Location      = "eastasia",
    [string]$ResourceGroup = "heat-strain-rg",
    [string]$HubName       = "heat-strain-hub",
    [string]$EdgeDeviceId  = "dorm-edge",
    [string]$AcrName       = "heatstrainacr",
    [string]$ModuleTag     = "v3"
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $here "..\..")

az extension add --upgrade -n azure-iot --only-show-errors | Out-Null
Write-Host "Account:" (az account show --query name -o tsv) -ForegroundColor Cyan

# --- Resource group ---------------------------------------------------------
az group create -n $ResourceGroup -l $Location --only-show-errors | Out-Null

# --- IoT Hub (Free F1) -------------------------------------------------------
$hubExists = az iot hub show -n $HubName -g $ResourceGroup --query name -o tsv 2>$null
if (-not $hubExists) {
    Write-Host "Creating IoT Hub $HubName (F1 free) ..." -ForegroundColor Green
    az iot hub create -n $HubName -g $ResourceGroup --sku F1 --partition-count 2 -l $Location --only-show-errors | Out-Null
} else { Write-Host "IoT Hub $HubName already exists." }

# --- IoT Edge device identity ----------------------------------------------
$devExists = az iot hub device-identity show -n $HubName -d $EdgeDeviceId --query deviceId -o tsv 2>$null
if (-not $devExists) {
    Write-Host "Registering IoT Edge device $EdgeDeviceId ..." -ForegroundColor Green
    az iot hub device-identity create -n $HubName -d $EdgeDeviceId --edge-enabled --only-show-errors | Out-Null
} else { Write-Host "Edge device $EdgeDeviceId already exists." }
$edgeConn = az iot hub device-identity connection-string show -n $HubName -d $EdgeDeviceId --query connectionString -o tsv

# --- Azure Container Registry (Basic, admin enabled) ------------------------
$acrExists = az acr show -n $AcrName --query name -o tsv 2>$null
if (-not $acrExists) {
    Write-Host "Creating ACR $AcrName (Basic) ..." -ForegroundColor Green
    az acr create -n $AcrName -g $ResourceGroup --sku Basic --admin-enabled true --only-show-errors | Out-Null
} else { Write-Host "ACR $AcrName already exists." }
$acrServer = az acr show -n $AcrName --query loginServer -o tsv
$acrUser   = az acr credential show -n $AcrName --query username -o tsv
$acrPass   = az acr credential show -n $AcrName --query "passwords[0].value" -o tsv

# --- Write cloud/azure/.env -------------------------------------------------
$dataPath = ((Join-Path $repoRoot "data") -replace '\\','/')
$envOut = @"
ACR_NAME=$AcrName
ACR_ADDRESS=$acrServer
ACR_USERNAME=$acrUser
ACR_PASSWORD=$acrPass
MODULE_IMAGE=$acrServer/posture-api:$ModuleTag
HOST_DATA_PATH=$dataPath
IOT_HUB_NAME=$HubName
EDGE_DEVICE_ID=$EdgeDeviceId
EDGE_CONNECTION_STRING=$edgeConn
"@
$envPath = Join-Path $here ".env"
$envOut | Set-Content -Path $envPath -Encoding utf8
Write-Host "`nWrote $envPath" -ForegroundColor Green
Write-Host "Next: .\cloud\azure\build_and_push.ps1  ->  gen_deployment.ps1  ->  run_edge.ps1" -ForegroundColor Cyan
