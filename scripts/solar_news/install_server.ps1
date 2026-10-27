param(
    [ValidateSet('install', 'share-login', 'connect', 'launch')][string]$Action = 'install',
    [switch]$ShareWindLogin
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
if ($ShareWindLogin) { $Action = 'share-login' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
$sha = (& git -C $source rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $sha -notmatch '^[0-9a-f]{40}$') { throw 'Invalid release commit' }
$installer = @{
    'install' = 'install_linux.sh'
    'share-login' = 'share_wind_login_linux.sh'
    'connect' = 'connect_linux.sh'
    'launch' = 'launch_linux.sh'
}[$Action]
& wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/solar_news/$installer" $linuxSource $sha
if ($LASTEXITCODE -ne 0) { throw "Solar $Action failed; inspect sanitized step output" }
