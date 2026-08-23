$ErrorActionPreference = "Stop"

$Project = "/home/mapworker/map-generator-simple"
$Cache = "/home/mapworker/map-cache/hot"
$Manifest = "data/showcase_pbf_manifest_20260822.json"
$LogDir = Join-Path $env:USERPROFILE "map_cluster_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq "wsl.exe" -and
    ($_.CommandLine -like "*generate_showcase_samples.py*" -or
     $_.CommandLine -like "*promote_staged_pbfs.py*")
}
if ($existing) {
    $existing | Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Depth 3
    throw "A Windows-hosted showcase process is already running"
}

$Prefix = @("-d", "Ubuntu-24.04", "--cd", $Project, "--")

function Start-WslJob {
    param(
        [string]$Name,
        [string[]]$LinuxArguments
    )
    $stdout = Join-Path $LogDir ($Name + ".out.log")
    $stderr = Join-Path $LogDir ($Name + ".err.log")
    $process = Start-Process -FilePath "wsl.exe" `
        -ArgumentList ($Prefix + $LinuxArguments) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    return [PSCustomObject]@{
        name = $Name
        pid = $process.Id
        stdout = $stdout
        stderr = $stderr
    }
}

$Initial = @(
    "/usr/bin/env",
    "MAP_GEN_CACHE_DIR=$Cache",
    "OSMIUM_BIN=/usr/bin/osmium",
    ".venv/bin/python", "tools/generate_showcase_samples.py",
    "--size-km", "25",
    "--only", "hangzhou",
    "--min-free-gb", "8"
)

$Files = @(
    "kanto-latest.osm.pbf",
    "nord-ovest-latest.osm.pbf",
    "malaysia-singapore-brunei-latest.osm.pbf",
    "south-korea-latest.osm.pbf",
    "new-south-wales-latest.osm.pbf",
    "victoria-latest.osm.pbf",
    "mexico-latest.osm.pbf",
    "argentina-latest.osm.pbf",
    "cataluna-latest.osm.pbf",
    "south-africa-latest.osm.pbf",
    "jiangsu-latest.osm.pbf"
) -join ","

$Promoter = @(
    ".venv/bin/python", "tools/promote_staged_pbfs.py",
    "--source-dir", "/mnt/c/Users/kiwi/pbf_stage",
    "--dest-dir", "$Project/pbf_cache",
    "--manifest", $Manifest,
    "--files", $Files,
    "--wait-seconds", "43200",
    "--poll-seconds", "30"
)

$Continuation = @(
    "/usr/bin/env",
    "MAP_GEN_CACHE_DIR=$Cache",
    "OSMIUM_BIN=/usr/bin/osmium",
    ".venv/bin/python", "tools/generate_showcase_samples.py",
    "--size-km", "25",
    "--only", "tokyo,milan,singapore,seoul,sydney,melbourne,mexico_city,buenos_aires,barcelona,cape_town,suzhou",
    "--min-free-gb", "8",
    "--pbf-size-manifest", $Manifest,
    "--wait-seconds", "43200",
    "--poll-seconds", "30"
)

$Jobs = @(
    Start-WslJob -Name "showcase-windows-initial" -LinuxArguments $Initial
    Start-WslJob -Name "showcase-windows-promoter" -LinuxArguments $Promoter
    Start-WslJob -Name "showcase-windows-continuation" -LinuxArguments $Continuation
)
$Jobs | ConvertTo-Json -Depth 3

# Keep the scheduled-task parent alive. Windows OpenSSH places descendants in
# a session job and reaps detached wsl.exe children when the SSH command exits.
Wait-Process -Id ($Jobs | ForEach-Object { $_.pid })
