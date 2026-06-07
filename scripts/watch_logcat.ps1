param(
    [string]$Device = "",
    [string]$PackageName = "com.smartwarehouse.wear_os_app",
    [switch]$ErrorsOnly,
    [switch]$ClearBuffer,
    [switch]$DumpOnce,
    [int]$Lines = 200
)

$ErrorActionPreference = "Stop"

if (-not $Device) {
    $Device = adb devices |
        Select-String "\sdevice$" |
        ForEach-Object { ($_ -split "\s+")[0] } |
        Select-Object -First 1
}

if (-not $Device) {
    throw "No online adb device found. Connect watch first (adb connect <ip:port>)."
}

Write-Host "Using device: $Device"

if ($ClearBuffer) {
    Write-Host "Clearing old logcat buffer..."
    adb -s $Device logcat -c
}

Write-Host "Collecting filtered logs for $PackageName. Press Ctrl+C to stop."
$pattern = "AndroidRuntime|FATAL EXCEPTION|Exception|SocketException|TimeoutException|$PackageName|flutter"

if ($DumpOnce) {
    Write-Host "Dumping recent filtered logs (app process not currently running)..."
    if ($ErrorsOnly) {
        adb -s $Device logcat -d -v time "*:E" | Select-String -Pattern $pattern | Select-Object -Last $Lines
    }
    else {
        adb -s $Device logcat -d -v time | Select-String -Pattern $pattern | Select-Object -Last $Lines
    }
    exit
}

if ($ErrorsOnly) {
    adb -s $Device logcat -v time "*:E" | Select-String -Pattern $pattern
}
else {
    adb -s $Device logcat -v time | Select-String -Pattern $pattern
}
