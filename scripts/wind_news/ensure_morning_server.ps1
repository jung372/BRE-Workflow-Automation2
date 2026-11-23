# Installed outside the runner checkout. No secrets or account changes.
param([DateTime]$NowUtc = [DateTime]::UtcNow, [string]$StateDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$zone = [TimeZoneInfo]::FindSystemTimeZoneById('Korea Standard Time')
$now = [TimeZoneInfo]::ConvertTimeFromUtc($NowUtc.ToUniversalTime(), $zone)
if ($now.TimeOfDay -lt [TimeSpan]::FromHours(7) -or $now.TimeOfDay -ge [TimeSpan]::FromHours(9.5)) {
    Write-Output 'MORNING_GUARD_OUTSIDE_WINDOW'
    exit 0
}
$day = $now.ToString('yyyy-MM-dd')
$preflight = $now.TimeOfDay -ge [TimeSpan]::FromMinutes(500) -and $now.TimeOfDay -lt [TimeSpan]::FromMinutes(510)
$deliveryCheck = $now.TimeOfDay -ge [TimeSpan]::FromMinutes(515) -and $now.TimeOfDay -lt [TimeSpan]::FromMinutes(525)
$news = @{'bre-wind-news-news-service-1' = 'wind'; 'bre-solar-news-solar-news-service-1' = 'solar'}

function Write-MorningStatus($industry, $status) {
    $stage = if ($status) { $status.stage } else { 'SERVICE_UNAVAILABLE' }
    $record = "$day $($now.ToString('HH:mm')) KST $industry $stage first=$($status.first_slot_job)/$($status.first_slot_result) delivery=$($status.delivery_status) claimed=$($status.delivery_claimed_at) web_verified_at=$($status.verified_at)"
    Add-Content -LiteralPath (Join-Path $StateDirectory 'morning-status.log') -Value $record -Encoding ASCII
    if ($preflight -and (!$status -or !$status.web_verified)) {
        Write-Warning $record
        $alert = Join-Path $StateDirectory "alert-$day-$industry.log"
        if (!(Test-Path -LiteralPath $alert)) {
            Set-Content -LiteralPath $alert -Value $record -Encoding ASCII
            $user = ([string]$supervisor.Principal.UserId -split '\\')[-1]
            if ($user -match '^[A-Za-z0-9_.@-]+$' -and (Get-Command msg.exe -ErrorAction SilentlyContinue)) {
                & msg.exe $user /TIME:60 "News briefing 08:30 KST readiness: $industry $stage" *> $null
            }
        }
    }
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
if ($LASTEXITCODE -ne 0) {
    if ($preflight) { foreach ($industry in $news.Values) { Write-MorningStatus $industry $null } }
    throw 'MORNING_DOCKER_NOT_READY'
}
$available = @()
foreach ($container in @('n8n-personal-postgres-1', 'n8n-personal-n8n-runner-1', 'n8n-personal-n8n-1') + @($news.Keys)) {
    $running = & wsl -d Ubuntu-24.04 --exec docker inspect --format '{{.State.Running}}' $container 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($container -eq 'bre-solar-news-solar-news-service-1') { continue }
        throw 'MORNING_CONTAINER_MISSING'
    }
    if ($news.ContainsKey($container)) { $available += $container }
    if (($running -join '').Trim() -ne 'true') {
        & wsl -d Ubuntu-24.04 --exec docker start $container *> $null
        if ($LASTEXITCODE -ne 0) { throw 'MORNING_CONTAINER_START_FAILED' }
    }
}
$unhealthy = @()
foreach ($container in @('n8n-personal-n8n-1') + $available) {
    $health = & wsl -d Ubuntu-24.04 --exec docker inspect --format '{{.State.Health.Status}}' $container
    if ($LASTEXITCODE -ne 0) { $unhealthy += $container; continue }
    $health = ($health -join '').Trim()
    if ($health -eq 'unhealthy' -and $news.ContainsKey($container)) {
        $restartLog = Join-Path $StateDirectory "restarts-$day-$($news[$container]).log"
        $attempts = if (Test-Path -LiteralPath $restartLog) { @(Get-Content -LiteralPath $restartLog).Count } else { 0 }
        if ($attempts -lt 2) {
            Add-Content -LiteralPath $restartLog -Value $now.ToString('o') -Encoding ASCII
            & wsl -d Ubuntu-24.04 --exec docker restart -t 30 $container *> $null
            if ($LASTEXITCODE -eq 0) { Write-Output "MORNING_NEWS_RESTARTED=$($news[$container])" }
        } else {
            Write-Warning "MORNING_NEWS_RESTART_LIMIT=$($news[$container])"
        }
    }
    if ($health -ne 'healthy') { $unhealthy += $container }
}

if ($preflight -or $deliveryCheck) {
    $probe = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'probe_morning.py') -Raw
    foreach ($container in $available) {
        $industry = $news[$container]
        $result = $probe | & wsl -d Ubuntu-24.04 --exec docker exec -i $container python - $industry 2>$null
        $status = if ($LASTEXITCODE -eq 0) { ($result -join '') | ConvertFrom-Json } else { $null }
        Write-MorningStatus $industry $status
    }
}
if ($unhealthy.Count) { throw ('MORNING_SERVICE_NOT_HEALTHY ' + ($unhealthy -join ',')) }
Write-Output 'MORNING_SERVICES_HEALTHY'
