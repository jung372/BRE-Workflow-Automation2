$ErrorActionPreference = 'Stop'
function Invoke-SolarWsl([string[]]$Arguments, [string]$InputText = '', [switch]$ShowOutput) {
    # All arguments below are fixed words or validated paths/identifiers.
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = 'wsl.exe'
    $info.Arguments = ($Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardInputEncoding = [System.Text.UTF8Encoding]::new($false)
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardInput.Write($InputText)
        $process.StandardInput.Close()
        if (-not $process.WaitForExit(120000)) { $process.Kill(); throw 'SOLAR_COMMAND_TIMEOUT' }
        $output = $stdout.GetAwaiter().GetResult()
        [void]$stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'SOLAR_COMMAND_FAILED' }
        if ($ShowOutput) { Write-Output $output.Trim() }
    } finally { $process.Dispose() }
}
$mode = $env:SOLAR_NEWS_REPORT_MODE
if ($mode -notin @('configure', 'preview', 'diagnose', 'send', 'test')) { throw 'INVALID_SOLAR_MODE' }
$distro = if ($env:SOLAR_NEWS_WSL_DISTRO) { $env:SOLAR_NEWS_WSL_DISTRO } else { 'Ubuntu-24.04' }
if ($distro -notmatch '^[A-Za-z0-9_.-]+$') { throw 'INVALID_SOLAR_WSL_ID' }
$wslArgs = @('--distribution', $distro)
if ($env:SOLAR_NEWS_WSL_USER) {
    if ($env:SOLAR_NEWS_WSL_USER -notmatch '^[A-Za-z0-9_.-]+$') { throw 'INVALID_SOLAR_WSL_ID' }
    $wslArgs += @('--user', $env:SOLAR_NEWS_WSL_USER)
}
$wslArgs += '--exec'
if ($mode -eq 'configure') {
  try {
    # Positional WSL arguments only. Signed URLs travel on stdin, never argv.
    if ([string]::IsNullOrWhiteSpace($env:SOLAR_NEWS_TEAMS_WEBHOOK_URL)) { throw 'SOLAR_NEWS_TEAMS_WEBHOOK_URL Secret is required' }
    $format = if ($env:SOLAR_NEWS_TEAMS_CARD_FORMAT) { $env:SOLAR_NEWS_TEAMS_CARD_FORMAT } else { 'messagecard' }
    if ($format -notin @('adaptive', 'messagecard')) { throw 'INVALID_SOLAR_CARD_FORMAT' }
    $source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
    $linuxSource = (& wsl @wslArgs wslpath -a $source).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'SOLAR_PATH_CONVERSION_FAILED' }
    $configureArgs = @('python3', ($linuxSource + '/scripts/solar_news/configure_teams.py'), '--apply', '--card-format', $format)
    foreach ($pair in @(@('--settings', $env:SOLAR_NEWS_SETTINGS_FILE), @('--release', $env:SOLAR_NEWS_RELEASE_ROOT))) {
        if ($pair[1]) {
            if ($pair[1] -notmatch '^/[A-Za-z0-9_./-]+$' -or $pair[1] -match '(^|/)\.\.(/|$)') { throw 'INVALID_SOLAR_PRIVATE_PATH' }
            $configureArgs += $pair
        }
    }
    Invoke-SolarWsl -Arguments ($wslArgs + $configureArgs) -InputText $env:SOLAR_NEWS_TEAMS_WEBHOOK_URL
    $env:SOLAR_NEWS_TEAMS_WEBHOOK_URL = $null
    Write-Output 'SOLAR_CONFIGURATION_APPLIED'
    exit 0
  } catch {
    Write-Output 'SOLAR_CONFIGURATION_FAILED'
    exit 1
  }
}
$reportArgs = @('--mode', $mode)
if ($env:SOLAR_NEWS_REPORT_DATE) { $reportArgs += @('--date', $env:SOLAR_NEWS_REPORT_DATE) }
try {
    # Reuse the installed container token without duplicating it in GitHub Secrets.
    $client = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'report.py') -Raw -Encoding UTF8
    Invoke-SolarWsl -Arguments ($wslArgs + @('docker', 'exec', '-i', 'bre-solar-news-solar-news-service-1', 'python', '-') + $reportArgs) -InputText $client -ShowOutput
} catch { Write-Output 'SOLAR_REPORT_FAILED'; exit 1 }
