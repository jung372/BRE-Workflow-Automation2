$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$mode = if ($env:NEWS_REPORT_MODE) { $env:NEWS_REPORT_MODE } else { 'preview' }
$day = $env:NEWS_REPORT_DATE
if ($mode -notin @('preview', 'send', 'scheduled', 'diagnose')) { throw 'Invalid report mode' }
if ($day -and $day -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'Invalid issue date' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
if ($mode -eq 'diagnose') {
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    Write-Output ('SERVER_LAST_BOOT=' + $boot.ToUniversalTime().ToString('o'))
    Get-ScheduledTask | Where-Object { $_.TaskName -match 'n8n|WSL|Docker|BRE' } | ForEach-Object {
        $info = $_ | Get-ScheduledTaskInfo
        Write-Output ('SERVER_TASK=' + (@{name=$_.TaskName; state=[string]$_.State; logon_type=[string]$_.Principal.LogonType;
            wake=$_.Settings.WakeToRun; execution_limit=$_.Settings.ExecutionTimeLimit; restart_count=$_.Settings.RestartCount;
            last_run=$info.LastRunTime.ToUniversalTime().ToString('o'); result=$info.LastTaskResult;
            triggers=@($_.Triggers | ForEach-Object { @{type=$_.CimClass.CimClassName; start=$_.StartBoundary; interval=$_.Repetition.Interval} })} | ConvertTo-Json -Compress -Depth 5))
    }
    foreach ($provider in @('Microsoft-Windows-Kernel-Power', 'Microsoft-Windows-Power-Troubleshooter', 'Microsoft-Windows-Kernel-General')) {
        Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName=$provider; StartTime=(Get-Date).AddDays(-2)} -ErrorAction SilentlyContinue |
            Where-Object { $_.Id -in @(1, 12, 13, 42, 107, 506, 507) } | Select-Object -First 20 |
            ForEach-Object { Write-Output ('SERVER_POWER_EVENT=' + (@{at=$_.TimeCreated.ToUniversalTime().ToString('o'); provider=$_.ProviderName; id=$_.Id} | ConvertTo-Json -Compress)) }
    }
    & wsl -d Ubuntu-24.04 --exec docker inspect --format 'NEWS_CONTAINER_STARTED={{.State.StartedAt}} RESTARTS={{.RestartCount}}' bre-wind-news-news-service-1
    & wsl -d Ubuntu-24.04 --exec docker inspect --format 'N8N_CONTAINER_STARTED={{.State.StartedAt}} RESTARTS={{.RestartCount}}' n8n-personal-n8n-1
    & wsl -d Ubuntu-24.04 --exec docker ps -a --format '{{.Names}}: {{.State}}'
}
# Feed the shared GitHub Secret through stdin, never argv, logs or checkout files.
if ($mode -ne 'diagnose') {
if (-not $env:TEAMS_WEBHOOK_URL) { throw 'TEAMS_WEBHOOK_URL GitHub Secret is required' }
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = 'wsl.exe'
$start.Arguments = "-d Ubuntu-24.04 --exec python3 `"$linuxSource/scripts/wind_news/configure_teams.py`""
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$process = [System.Diagnostics.Process]::Start($start)
$process.StandardInput.Write($env:TEAMS_WEBHOOK_URL)
$process.StandardInput.Close()
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw 'Shared Teams configuration failed; no message sent' }
$process.Dispose()
}
$result = & wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/wind_news/report_linux.sh" $linuxSource $mode $day
$reportExit = $LASTEXITCODE
$result | ForEach-Object { Write-Output $_ }
$jsonLine = $result | Where-Object { $_ -like 'NEWS_BRIEFING_RESULT=*' } | Select-Object -Last 1
if ($jsonLine -and $env:GITHUB_STEP_SUMMARY) {
    $report = $jsonLine.Substring('NEWS_BRIEFING_RESULT='.Length) | ConvertFrom-Json
    $summary = @(
        '## 뉴스 브리핑 Teams 보고',
        '',
        "- 날짜: $($report.issue_date)",
        "- 실행 방식: $mode",
        "- 결과: $($report.status)",
        "- Teams 연결 설정: $($report.teams_configured)",
        "- 카드 형식: $($report.card_format)",
        "- 기존 발송 상태: $($report.delivery_status)",
        "- 사유: $($report.error_code)",
        "- HTTP 상태: $($report.http_status)",
        "- 수신 측 오류: $($report.receiver_error_code)",
        "- 후속 조치: $($report.operator_action)",
        '',
        "[웹에서 해당 날짜 브리핑 보기](https://jung372.github.io/BRE-Workflow-Automation2/#/daily/$($report.issue_date))"
    )
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value $summary -Encoding UTF8
}
if ($reportExit -ne 0) { throw 'News briefing failed; inspect safe status and error code above' }
