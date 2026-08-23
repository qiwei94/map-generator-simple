$ErrorActionPreference = "Stop"

$TaskName = "MapShowcase25Repairs-20260823"
$ScriptPath = Join-Path $env:USERPROFILE "start_windows_showcase_repairs.ps1"
if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Missing launcher: $ScriptPath"
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    $existing | Select-Object TaskName, State | ConvertTo-Json
    throw "The repair task is already running"
}

$argument = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5)
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 18) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName `
    -Description "One-time corrected 25 km showcase rerun" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[PSCustomObject]@{
    task = $task.TaskName
    state = $task.State.ToString()
    last_run_time = $info.LastRunTime
    last_result = $info.LastTaskResult
    execution_limit_hours = 18
} | ConvertTo-Json
