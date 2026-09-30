param([ValidateSet('install', 'connect', 'verify', 'launch')][string]$Action = 'install')
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$source = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxSource) { throw 'WSL path conversion failed' }
$sha = (& git -C $source rev-parse HEAD).Trim()
& wsl -d Ubuntu-24.04 --exec bash "$linuxSource/scripts/wind_news/${Action}_linux.sh" $linuxSource $sha
if ($LASTEXITCODE -ne 0) { throw 'News installation failed; inspect sanitized step output' }
