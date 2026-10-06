$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$mode = if ($env:NEWS_REPORT_MODE) { $env:NEWS_REPORT_MODE } else { 'preview' }
$day = $env:NEWS_REPORT_DATE
if ($mode -notin @('preview', 'send', 'scheduled')) { throw 'Invalid report mode' }
if ($day -and $day -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'Invalid issue date' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
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
        "- 사유: $($report.error_code)",
        '',
        "[웹에서 해당 날짜 브리핑 보기](https://jung372.github.io/BRE-Workflow-Automation2/#/daily/$($report.issue_date))"
    )
    Add-Content -LiteralPath $env:GITHUB_STEP_SUMMARY -Value $summary -Encoding UTF8
}
if ($reportExit -ne 0) { throw 'News briefing failed; inspect safe status and error code above' }
