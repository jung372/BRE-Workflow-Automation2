# Installed outside the runner checkout. No secrets or account changes.
param([DateTime]$NowUtc = [DateTime]::UtcNow)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$zone = [TimeZoneInfo]::FindSystemTimeZoneById('Korea Standard Time')
$now = [TimeZoneInfo]::ConvertTimeFromUtc($NowUtc.ToUniversalTime(), $zone)
if ($now.TimeOfDay -lt [TimeSpan]::FromHours(7) -or $now.TimeOfDay -ge [TimeSpan]::FromHours(9.5)) {
    Write-Output 'MORNING_GUARD_OUTSIDE_WINDOW'
    exit 0
}
# Reuse the existing supervisor; it keeps the WSL instance alive.
$supervisor = Get-ScheduledTask -TaskName 'Personal n8n Supervisor'
if ($supervisor.State -ne 'Running') {
    Start-ScheduledTask -TaskName $supervisor.TaskName
}
# systemd starts Docker on WSL startup. Probe only the existing containers.
for ($attempt = 0; $attempt -lt 6; $attempt++) {
    & wsl -d Ubuntu-24.04 --exec docker info --format '{{.ServerVersion}}' *> $null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 5
}
if ($LASTEXITCODE -ne 0) { throw 'MORNING_DOCKER_NOT_READY' }
foreach ($container in @('n8n-personal-postgres-1', 'n8n-personal-n8n-runner-1', 'n8n-personal-n8n-1', 'bre-wind-news-news-service-1')) {
    $running = & wsl -d Ubuntu-24.04 --exec docker inspect --format '{{.State.Running}}' $container
    if ($LASTEXITCODE -ne 0) { throw 'MORNING_CONTAINER_MISSING' }
    if (($running -join '').Trim() -ne 'true') {
        & wsl -d Ubuntu-24.04 --exec docker start $container *> $null
        if ($LASTEXITCODE -ne 0) { throw 'MORNING_CONTAINER_START_FAILED' }
    }
}
foreach ($container in @('n8n-personal-n8n-1', 'bre-wind-news-news-service-1')) {
    $health = & wsl -d Ubuntu-24.04 --exec docker inspect --format '{{.State.Health.Status}}' $container
    if ($LASTEXITCODE -ne 0 -or ($health -join '').Trim() -ne 'healthy') {
        throw 'MORNING_SERVICE_NOT_HEALTHY; next scheduled check will retry'
    }
}
Write-Output 'MORNING_SERVICES_HEALTHY'
