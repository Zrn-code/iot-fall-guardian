# Render deployment.template.json -> deployment.generated.json by substituting
# ${VARS} from cloud/azure/.env. Pure text substitution so the escaped
# createOptions JSON strings are preserved exactly.
#
#   .\cloud\azure\gen_deployment.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$envFile = Join-Path $here ".env"
if (-not (Test-Path $envFile)) {
    throw "cloud/azure/.env not found. Copy .env.example to .env and fill it in."
}

# Parse KEY=VALUE lines (ignore blanks/comments).
$vars = @{}
foreach ($line in Get-Content $envFile) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $kv = $t -split "=", 2
    if ($kv.Count -eq 2) { $vars[$kv[0].Trim()] = $kv[1].Trim() }
}

$template = Get-Content (Join-Path $here "deployment.template.json") -Raw
foreach ($k in $vars.Keys) {
    $template = $template.Replace('${' + $k + '}', $vars[$k])
}

# Warn on any un-substituted placeholders.
$leftover = [regex]::Matches($template, '\$\{[A-Z_]+\}') | ForEach-Object { $_.Value } | Sort-Object -Unique
if ($leftover) { Write-Warning "Un-substituted placeholders: $($leftover -join ', ')" }

# Validate it parses as JSON before writing.
try { $null = $template | ConvertFrom-Json } catch { throw "Generated content is not valid JSON: $_" }

$out = Join-Path $here "deployment.generated.json"
$template | Set-Content -Path $out -Encoding utf8 -NoNewline
Write-Host "wrote $out" -ForegroundColor Green
