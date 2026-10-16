param([switch]$ShareWindLogin)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
$sha = (& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sha -notmatch '^[0-9a-f]{40}$') { throw 'Invalid release commit' }
$installer = if ($ShareWindLogin) { 'share_wind_login_linux.sh' } else { 'install_linux.sh' }
& wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/solar_news/$installer" $linuxSource $sha
if ($LASTEXITCODE -ne 0) { throw 'Solar installation failed; inspect sanitized step output' }
