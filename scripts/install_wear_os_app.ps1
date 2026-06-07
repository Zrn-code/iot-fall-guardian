param(
    [string]$WatchEndpoint = "",
    [string]$ProjectRoot = "",
    [string]$PackageName = "com.smartwarehouse.wear_os_app"
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

$wearAppDir = Join-Path $ProjectRoot "wear_os_app"
$apkPath = Join-Path $wearAppDir "build\app\outputs\flutter-apk\app-debug.apk"

if (-not (Test-Path $wearAppDir)) {
    throw "wear_os_app directory not found: $wearAppDir"
}

Write-Host "[1/6] Starting adb server..."
adb start-server | Out-Null

if ($WatchEndpoint) {
    Write-Host "[2/6] Connecting to watch: $WatchEndpoint"
    adb connect $WatchEndpoint | Out-Host
}

Write-Host "[3/6] Resolving online adb device..."
$device = adb devices |
    Select-String "\sdevice$" |
    ForEach-Object { ($_ -split "\s+")[0] } |
    Select-Object -First 1

if (-not $device) {
    throw "No online adb device found. Use -WatchEndpoint <ip:port> or reconnect the watch first."
}

Write-Host "Using device: $device"

Push-Location $wearAppDir
try {
    Write-Host "[4/6] flutter pub get"
    flutter pub get

    Write-Host "[5/6] Building debug APK"
    flutter build apk --debug
}
finally {
    Pop-Location
}

if (-not (Test-Path $apkPath)) {
    throw "APK not found after build: $apkPath"
}

Write-Host "[6/6] Installing APK"
adb -s $device install -r $apkPath | Out-Host

$installed = adb -s $device shell pm list packages | Select-String $PackageName
if (-not $installed) {
    throw "Install may have failed: package $PackageName not found on device"
}

Write-Host "Done. Installed $PackageName on $device"
