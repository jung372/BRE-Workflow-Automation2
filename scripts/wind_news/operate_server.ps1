param([ValidateSet('install', 'connect', 'verify', 'launch', 'correct', 'refresh', 'dedup-correct', 'briefing')][string]$Action = 'install')
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
$sha = (& git -C $source rev-parse HEAD).Trim()
if ($Action -eq 'refresh' -or $Action -eq 'dedup-correct') {
    & wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/wind_news/install_linux.sh" $linuxSource $sha
    if ($LASTEXITCODE -ne 0) { throw 'News refresh installation failed' }
    if ($Action -eq 'refresh') { $Action = 'correct' }
}
$scriptAction = if ($Action -eq 'correct') { 'launch' } else { $Action }
& wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/wind_news/${scriptAction}_linux.sh" $linuxSource $sha $Action
if ($LASTEXITCODE -ne 0) { throw 'News installation failed; inspect sanitized step output' }
