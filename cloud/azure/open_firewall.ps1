# Allow inbound TCP 8000 (posture-api module) + 18080 (ThingsBoard) so the watch
# and phone on the campus network can reach this dorm edge node (140.113.123.43).
# RUN AS ADMINISTRATOR.
#
#   .\cloud\azure\open_firewall.ps1
$ErrorActionPreference = "Stop"

$rules = @(
    @{ Name = "HeatStrain-postureApi-8000"; Port = 8000;  Desc = "posture-api IoT Edge module" },
    @{ Name = "HeatStrain-ThingsBoard-18080"; Port = 18080; Desc = "ThingsBoard HTTP" }
)

foreach ($r in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Rule '$($r.Name)' already exists." -ForegroundColor Yellow
        continue
    }
    New-NetFirewallRule -DisplayName $r.Name -Description $r.Desc `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $r.Port `
        -Profile Any | Out-Null
    Write-Host "Added inbound rule: $($r.Name) (TCP $($r.Port))" -ForegroundColor Green
}

Write-Host "`nFrom a phone on the SAME (campus) network, verify reachability:" -ForegroundColor Cyan
Write-Host "  http://140.113.123.43:8000/health"
Write-Host "  http://140.113.123.43:18080   (ThingsBoard login)"
