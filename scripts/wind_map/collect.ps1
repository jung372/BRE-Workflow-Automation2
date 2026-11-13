param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Runtime,
    [Parameter(Mandatory=$true)][string]$Sources,
    [int]$MaxJobs=5
)
$ErrorActionPreference='Stop'
$sourceRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
Set-Location -LiteralPath $sourceRoot
& $Python -m wind_map --runtime $Runtime cycle --sources $Sources --live --max-jobs $MaxJobs
exit $LASTEXITCODE
