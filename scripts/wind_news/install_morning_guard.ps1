# Called only by the explicitly selected news_action=briefing deployment.
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$supervisor = Get-ScheduledTask -TaskName 'Personal n8n Supervisor'
if (-not $supervisor.Principal.UserId) { throw 'MORNING_SUPERVISOR_PRINCIPAL_MISSING' }
$taskName = 'BRE News Morning Guard'
$directory = Join-Path $env:LOCALAPPDATA 'BRE-Wind-News\operator'
New-Item -ItemType Directory -Path $directory -Force > $null
$script = Join-Path $directory 'ensure_morning_server.ps1'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $directory 'morning-guard.previous.xml') -Encoding UTF8
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ensure_morning_server.ps1') -Destination $script -Force
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At '07:00'
$trigger.StartBoundary = (Get-Date -Hour 7 -Minute 0 -Second 0).ToString('yyyy-MM-dd') + 'T07:00:00+09:00'
# Daily trigger + finite repetition; missed runs cannot run outside the script's KST window.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Minutes 150)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
# Preserve the existing WSL owner's logon/account contract; no password or token.
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $supervisor.Principal -Force > $null
$actual = Get-ScheduledTask -TaskName $taskName
if ($actual.Triggers[0].Repetition.Interval -ne 'PT5M' -or -not $actual.Settings.WakeToRun) { throw 'MORNING_GUARD_VERIFICATION_FAILED' }
Write-Output 'MORNING_GUARD_INSTALLED window=07:00-09:25 interval=5m wake=true'
Write-Output ('MORNING_GUARD_LOGON_TYPE=' + [string]$actual.Principal.LogonType)
