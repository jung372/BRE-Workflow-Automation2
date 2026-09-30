# Read-only inventory. Never dump environment variables, container environment,
# process command lines, credential files, or n8n's encryption key.
$ErrorActionPreference = 'Stop'
$report = [ordered]@{
    computer = $env:COMPUTERNAME
    identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    tools = @{}
    runtimeExists = Test-Path -LiteralPath 'D:\05 AI Study\BRE_Workflow_runtime'
}
foreach ($name in @('python', 'node', 'git', 'docker', 'wsl', 'codex')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    $report.tools[$name] = if ($command) { $command.Source } else { $null }
}
$report.services = @(Get-Service | Where-Object { $_.Name -match 'docker|n8n|wsl|sshd' } | ForEach-Object {
    @{ name = $_.Name; status = [string]$_.Status }
})
$report.listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(5678, 8090, 8444) } |
    Select-Object LocalAddress, LocalPort)
$report | ConvertTo-Json -Depth 5
if ($report.tools.wsl) {
    Write-Output '--- WSL distributions ---'
    & wsl --list --verbose
}
if ($report.tools.docker) {
    Write-Output '--- Docker engine ---'
    & docker version --format '{{.Server.Version}}'
    if ($LASTEXITCODE -eq 0) {
        Write-Output '--- Container inventory (no environment) ---'
        & docker ps --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}'
        $containers = @(& docker ps --format '{{.Names}}' | Where-Object { $_ -match 'n8n' })
        foreach ($container in $containers) {
            Write-Output "--- n8n mounts and networks: $container ---"
            & docker inspect --format '{{json .Mounts}}' $container
            & docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{$name}} {{end}}' $container
        }
    }
}
# Missing optional tools are findings, not a failed inventory job.
exit 0
