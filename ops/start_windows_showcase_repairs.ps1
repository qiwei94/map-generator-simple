$ErrorActionPreference = "Stop"

$Project = "/home/mapworker/map-generator-simple"
$Cache = "/home/mapworker/map-cache/hot"
$Manifest = "data/showcase_pbf_manifest_20260822.json"
$LogDir = Join-Path $env:USERPROFILE "map_cluster_logs"
$Name = "showcase-windows-repairs-20260823"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "wsl.exe" -and
    $_.CommandLine -like "*generate_showcase_samples.py*"
}
if ($existing) {
    $existing | Select-Object ProcessId, Name, CommandLine |
        ConvertTo-Json -Depth 3
    throw "A Windows-hosted showcase process is already running"
}

$arguments = @(
    "-d", "Ubuntu-24.04",
    "--cd", $Project,
    "--",
    "/usr/bin/env",
    "MAP_GEN_CACHE_DIR=$Cache",
    "OSMIUM_BIN=/usr/bin/osmium",
    ".venv/bin/python", "tools/generate_showcase_samples.py",
    "--size-km", "25",
    "--only", "sydney,melbourne,mexico_city,buenos_aires,cape_town",
    "--min-free-gb", "20",
    "--pbf-size-manifest", $Manifest,
    "--force"
)

$stdout = Join-Path $LogDir ($Name + ".out.log")
$stderr = Join-Path $LogDir ($Name + ".err.log")
$process = Start-Process -FilePath "wsl.exe" `
    -ArgumentList $arguments `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

[PSCustomObject]@{
    name = $Name
    pid = $process.Id
    stdout = $stdout
    stderr = $stderr
    cities = @("sydney", "melbourne", "mexico_city", "buenos_aires", "cape_town")
} | ConvertTo-Json -Depth 3

# Keep the scheduled-task parent alive. Windows OpenSSH reaps detached WSL
# descendants when the SSH session exits.
Wait-Process -Id $process.Id
$process.Refresh()
exit $process.ExitCode
