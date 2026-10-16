$ErrorActionPreference = 'Stop'
function Invoke-SolarWsl([string[]]$Arguments, [string]$InputText = '') {
    # All arguments below are fixed words or validated paths/identifiers.
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = 'wsl.exe'
    $info.Arguments = $Arguments -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($InputText)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'SOLAR_COMMAND_TIMEOUT' }
        [void]$stdout.GetAwaiter().GetResult()
        [void]$stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'SOLAR_COMMAND_FAILED' }
    } finally { $process.Dispose() }
}
$mode = $env:SOLAR_NEWS_REPORT_MODE
if ($mode -notin @('configure', 'preview', 'diagnose', 'send', 'test')) { throw 'INVALID_SOLAR_MODE' }
if ($mode -eq 'configure') {
  try {
    # Positional WSL arguments only. Signed URLs travel on stdin, never argv.
    foreach ($name in @('SOLAR_NEWS_WSL_DISTRO', 'SOLAR_NEWS_WSL_USER', 'SOLAR_NEWS_RELEASE_ROOT', 'SOLAR_NEWS_SETTINGS_FILE', 'SOLAR_NEWS_TEAMS_WEBHOOK_URL')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) { throw 'SOLAR_SETTING_MISSING' }
    }
    foreach ($value in @($env:SOLAR_NEWS_RELEASE_ROOT, $env:SOLAR_NEWS_SETTINGS_FILE)) {
        if ($value -notmatch '^/[A-Za-z0-9_./-]+$' -or $value -match '(^|/)\.\.(/|$)') { throw 'INVALID_SOLAR_PRIVATE_PATH' }
    }
    foreach ($value in @($env:SOLAR_NEWS_WSL_DISTRO, $env:SOLAR_NEWS_WSL_USER)) {
        if ($value -notmatch '^[A-Za-z0-9_.-]+$') { throw 'INVALID_SOLAR_WSL_ID' }
    }
    if ($env:SOLAR_NEWS_TEAMS_CARD_FORMAT -notin @('adaptive', 'messagecard')) { throw 'SOLAR_CARD_FORMAT_REQUIRED' }
    $wslArgs = @('--distribution', $env:SOLAR_NEWS_WSL_DISTRO, '--user', $env:SOLAR_NEWS_WSL_USER, '--exec')
    $scriptPath = $env:SOLAR_NEWS_RELEASE_ROOT + '/scripts/solar_news/configure_teams.py'
    Invoke-SolarWsl -Arguments ($wslArgs + @('python3', $scriptPath, '--settings', $env:SOLAR_NEWS_SETTINGS_FILE, '--card-format', $env:SOLAR_NEWS_TEAMS_CARD_FORMAT)) -InputText $env:SOLAR_NEWS_TEAMS_WEBHOOK_URL
    $env:SOLAR_NEWS_TEAMS_WEBHOOK_URL = $null
    Invoke-SolarWsl -Arguments ($wslArgs + @('docker', 'compose', '--project-name', 'bre-solar-news', '--env-file', $env:SOLAR_NEWS_SETTINGS_FILE, '-f', ($env:SOLAR_NEWS_RELEASE_ROOT + '/infra/solar-news/compose.yaml'), 'up', '-d', '--no-build', '--no-deps', 'solar-news-service'))
    Write-Output 'SOLAR_CONFIGURATION_APPLIED'
    exit 0
  } catch {
    Write-Output 'SOLAR_CONFIGURATION_FAILED'
    exit 1
  }
}
$reportArgs = @('--mode', $mode)
if ($env:SOLAR_NEWS_REPORT_DATE) { $reportArgs += @('--date', $env:SOLAR_NEWS_REPORT_DATE) }
python (Join-Path $PSScriptRoot 'report.py') @reportArgs
exit $LASTEXITCODE
