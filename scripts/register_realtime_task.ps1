$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_realtime_preview.ps1"
$TaskName = "QuantRealtimePreview"

$Times = @(
    '09:45', '10:00', '10:15', '10:30', '10:45', '11:00', '11:15', '11:30',
    '13:15', '13:30', '13:45', '14:00', '14:15', '14:30', '14:45'
)
$Triggers = foreach ($Time in $Times) {
    $At = [datetime]::ParseExact($Time, 'HH:mm', $null)
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $At
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $Runner)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers -Settings $Settings `
    -Description "A-share intraday realtime preview every 15 minutes during trading sessions" -Force | Out-Null

$Task = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Output ("TaskName: {0}" -f $Task.TaskName)
Write-Output ("State: {0}" -f $Task.State)
Write-Output ("TriggerCount: {0}" -f $Task.Triggers.Count)
Write-Output ("NextRunTime: {0}" -f $Info.NextRunTime)
Write-Output ("Action: {0} {1}" -f $Task.Actions.Execute, $Task.Actions.Arguments)
