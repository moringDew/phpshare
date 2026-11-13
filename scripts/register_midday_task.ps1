$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_midday_check.ps1"
$TaskName = "QuantMiddayCheck"

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $Runner)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At 11:35
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "A-share midday execution check for the previous close-based trade alert" -Force | Out-Null

$Task = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("TaskName: {0}" -f $Task.TaskName)
Write-Output ("State: {0}" -f $Task.State)
Write-Output ("NextRunTime: {0}" -f $Info.NextRunTime)
Write-Output ("Action: {0} {1}" -f $Task.Actions.Execute, $Task.Actions.Arguments)
