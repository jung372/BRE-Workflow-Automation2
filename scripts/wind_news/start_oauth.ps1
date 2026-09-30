# No device code or long-lived credentials may appear in Actions logs/artifacts.
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-EVU6USL') { throw 'Unexpected server' }
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$state = (& $tailscale status --json | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw 'Tailscale is unavailable' }
$peer = @($state.Peer.PSObject.Properties.Value | Where-Object {
    $_.HostName -ieq 'nb01-PF4JSBDE' -and $_.Online -and $_.UserID -eq $state.Self.UserID
})
if ($peer.Count -ne 1) { throw 'The account owner PC must be online on the same tailnet' }
$target = $peer[0].TailscaleIPs[0]
if ($target -notmatch '^100\.\d+\.\d+\.\d+$') { throw 'Unexpected Taildrop destination' }
$container = 'bre-wind-news-news-service-1'
$source = (Resolve-Path (Join-Path $PSScriptRoot 'oauth_device.py')).Path.Replace('\', '/')
$linuxSource = (& wsl -d Ubuntu-24.04 --exec wslpath -a $source).Trim()
if ($LASTEXITCODE -ne 0) { throw 'WSL path conversion failed' }
$prepare = $linuxSource.Replace('/oauth_device.py', '/prepare_oauth_linux.sh')
& wsl -d Ubuntu-24.04 --exec bash $prepare $linuxSource
if ($LASTEXITCODE -ne 0) { throw 'Device helper installation failed' }
$session = [guid]::NewGuid().ToString('N')
& wsl -d Ubuntu-24.04 --exec docker exec -d -e CODEX_HOME=/var/lib/bre-wind/codex-auth $container python /tmp/bre-oauth-device.py run $session
if ($LASTEXITCODE -ne 0) { throw 'Device login could not start' }
$challenge = $null
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    # Captured in memory, never echoed. No command contains the code itself.
    $raw = & wsl -d Ubuntu-24.04 --exec docker exec $container python /tmp/bre-oauth-device.py challenge $session
    if ($LASTEXITCODE -eq 0) { $challenge = $raw | ConvertFrom-Json; break }
    Start-Sleep -Seconds 2
}
if (-not $challenge -or $challenge.url -ne 'https://auth.openai.com/codex/device' -or $challenge.code -notmatch '^[A-Z0-9]{4,6}-[A-Z0-9]{4,6}$') {
    throw 'No valid device challenge. Check device-code login availability; no raw output disclosed.'
}
$folder = Join-Path $env:RUNNER_TEMP "bre-oauth-$session"
New-Item -ItemType Directory -Path $folder | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $folder /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private challenge folder ACL failed' }
$file = Join-Path $folder "bre-wind-device-$session.json"
try {
    $challenge | ConvertTo-Json | Set-Content -LiteralPath $file -Encoding UTF8
    & $tailscale file cp --update-interval=0 $file "${target}:" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Private Taildrop delivery failed' }
    Write-Output "DEVICE_CHALLENGE_SENT=bre-wind-device-$session.json"
    Write-Output 'DEVICE_LOGIN_EXPIRES_WITHIN=12 minutes'
} finally {
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $folder -ErrorAction SilentlyContinue
    $raw = $null
    $challenge = $null
}
