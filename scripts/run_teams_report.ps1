[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\05 AI Study\BRE Workflow Automation',
    [string]$RuntimeRoot = 'D:\05 AI Study\BRE_Workflow_runtime',
    [switch]$Preview,
    [switch]$Manual,
    [switch]$Force,
    [DateTime]$NowUtc = [DateTime]::UtcNow
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$now = [TimeZoneInfo]::ConvertTimeFromUtc($NowUtc.ToUniversalTime(), [TimeZoneInfo]::FindSystemTimeZoneById('Korea Standard Time'))
if (-not $Preview -and -not $Manual) {
    if ($now.DayOfWeek -in @('Saturday', 'Sunday') -or $now.TimeOfDay -lt [TimeSpan]::Parse('08:05') -or $now.TimeOfDay -ge [TimeSpan]::Parse('09:05')) {
        Write-Output 'TEAMS_REPORT OUTSIDE_WINDOW'
        exit 0
    }
}
$release = (Get-Content -LiteralPath (Join-Path $InstallRoot 'current_release.txt') -Raw).Trim()
$release = [IO.Path]::GetFullPath($release)
$allowedRoot = [IO.Path]::GetFullPath((Join-Path $InstallRoot 'releases')) + [IO.Path]::DirectorySeparatorChar
if (-not $release.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'REPORT_RELEASE_OUTSIDE_ROOT' }
$python = Join-Path $release '.venv\Scripts\python.exe'
$app = Join-Path $release 'notify_teams.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $app)) { throw 'REPORT_RELEASE_MISSING' }
$env:BRE_DATA_DIR = Join-Path $RuntimeRoot 'publish'
$env:BRE_RUNTIME_DIR = $RuntimeRoot
$env:BRE_ENV_FILE = Join-Path $RuntimeRoot '.env'
$env:BRE_NODE_ROLE = 'server'
$arguments = @($app)
if ($Preview) { $arguments += '--preview' }
if ($Manual) { $arguments += '--manual' }
if ($Force) { $arguments += '--force' }
$logRoot = Join-Path $RuntimeRoot 'logs'
New-Item -ItemType Directory -Path $logRoot -Force > $null
$log = Join-Path $logRoot ('teams-report-' + $now.ToString('yyyy-MM-dd') + '.log')
try {
    if (-not $Preview) {
        # DPAPI binds this credential to the existing scraper task's account.
        $secret = Import-Clixml -LiteralPath (Join-Path $RuntimeRoot 'teams-report\webhook.clixml')
        $credential = New-Object System.Management.Automation.PSCredential('teams', $secret)
        $env:TEAMS_WEBHOOK_URL = $credential.GetNetworkCredential().Password
    }
    & $python @arguments 2>&1 | Tee-Object -FilePath $log -Append
    $result = $LASTEXITCODE
} finally {
    Remove-Item Env:\TEAMS_WEBHOOK_URL -ErrorAction SilentlyContinue
    $credential = $null
    $secret = $null
}
if ($result -eq 20 -and -not $Preview -and -not $Manual -and $now.TimeOfDay -le [TimeSpan]::Parse('08:40')) {
    # The scraper owns its existing lock and publish clone. Never start a
    # competing scraper, and leave sufficient time for its bounded run.
    $scraper = Get-ScheduledTask -TaskName 'BRE Scraper'
    if ($scraper.State -ne 'Running' -and -not (Test-Path -LiteralPath (Join-Path $RuntimeRoot 'deploy.lock'))) {
        Start-ScheduledTask -TaskName $scraper.TaskName
        'TEAMS_REPORT SNAPSHOT_RECOVERY_REQUESTED' | Tee-Object -FilePath $log -Append
    }
}
exit $result
