[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\05 AI Study\BRE Workflow Automation',
    [string]$RuntimeRoot = 'D:\05 AI Study\BRE_Workflow_runtime',
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'REPORT_UNEXPECTED_SERVER' }
$taskName = 'BRE Teams Morning Report'
$directory = Join-Path $RuntimeRoot 'teams-report'
$launcher = Join-Path $InstallRoot 'launcher\run_teams_report.ps1'
$release = (Get-Content -LiteralPath (Join-Path $InstallRoot 'current_release.txt') -Raw).Trim()
if (-not (Test-Path -LiteralPath (Join-Path $release 'logic\regular_report.py'))) { throw 'REPORT_DEPLOY_NEW_RELEASE_FIRST' }
if (-not $VerifyOnly) {
    $scraper = Get-ScheduledTask -TaskName 'BRE Scraper'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $taskAccount = New-Object Security.Principal.NTAccount($scraper.Principal.UserId)
    if ($taskAccount.Translate([Security.Principal.SecurityIdentifier]).Value -ne $identity.User.Value) { throw 'REPORT_TASK_ACCOUNT_MISMATCH' }
    if (-not $env:TEAMS_WEBHOOK_URL) { throw 'REPORT_SECRET_NOT_CONFIGURED' }
    New-Item -ItemType Directory -Path $directory -Force > $null
    # Store only a DPAPI-encrypted secret, outside release/publish. Restrict the
    # directory to this account and SYSTEM; never inspect or log its value.
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($identity.User, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $directory -AclObject $acl
    $secretPath = Join-Path $directory 'webhook.clixml'
    ConvertTo-SecureString $env:TEAMS_WEBHOOK_URL -AsPlainText -Force | Export-Clixml -LiteralPath ($secretPath + '.tmp')
    Move-Item -LiteralPath ($secretPath + '.tmp') -Destination $secretPath -Force
    Remove-Item Env:\TEAMS_WEBHOOK_URL -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $directory 'previous-task.xml') -Encoding UTF8
    }
    Copy-Item -LiteralPath (Join-Path $release 'scripts\run_teams_report.ps1') -Destination $launcher -Force
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`" -InstallRoot `"$InstallRoot`" -RuntimeRoot `"$RuntimeRoot`""
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:05'
    $trigger.StartBoundary = (Get-Date).ToString('yyyy-MM-dd') + 'T08:05:00+09:00'
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Minutes 60)).Repetition
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $scraper.Principal -Force > $null
}
$actual = Get-ScheduledTask -TaskName $taskName
if ($actual.Triggers[0].Repetition.Interval -ne 'PT5M' -or $actual.Triggers[0].Repetition.Duration -ne 'PT1H' -or
    $actual.Triggers[0].DaysOfWeek -ne 62 -or -not $actual.Settings.WakeToRun -or -not $actual.Settings.Enabled -or
    $actual.Triggers[0].StartBoundary -notlike '*T08:05:00+09:00' -or $actual.Actions[0].Arguments -notlike '*run_teams_report.ps1*') {
    throw 'REPORT_TASK_VERIFICATION_FAILED'
}
$encrypted = Import-Clixml -LiteralPath (Join-Path $directory 'webhook.clixml')
if ($encrypted -isnot [Security.SecureString] -or $encrypted.Length -eq 0) { throw 'REPORT_SECRET_VERIFICATION_FAILED' }
$encrypted = $null
Write-Output 'REPORT_SCHEDULE_VERIFIED owner=WindowsTask weekdays=Mon-Fri prepare=08:05 send=08:30-08:55 interval=5m deadline=09:00 timezone=Asia/Seoul wake=true'
Write-Output ('REPORT_LOGON_TYPE=' + [string]$actual.Principal.LogonType)
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Output ('REPORT_NEXT_RUN=' + $info.NextRunTime.ToString('s'))
# Preview reads actual publish data and the daily claim, but never calls Teams.
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $launcher -InstallRoot $InstallRoot -RuntimeRoot $RuntimeRoot -Preview
if ($LASTEXITCODE -ne 0) { throw 'REPORT_PREVIEW_FAILED' }
