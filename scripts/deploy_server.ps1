[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [string]$InstallRoot = 'D:\05 AI Study\BRE Workflow Automation',
    [string]$RuntimeRoot = 'D:\05 AI Study\BRE_Workflow_runtime',
    [string]$TaskName = 'BRE Scraper',
    [string]$CommitSha = 'manual',
    [int]$SmokeMinOk = 3,
    [int]$KeepReleases = 5,
    [switch]$SkipSmoke,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RunLockWaitSeconds = 25 * 60
$StaleLockSeconds = 30 * 60

function Write-Step {
    param([string]$Message)
    Write-Host "[DEPLOY] $Message"
}

function Resolve-PythonLauncher {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{ Executable = $py.Source; Prefix = @('-3') }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{ Executable = $python.Source; Prefix = @() }
    }
    throw 'Python 3 을 찾을 수 없습니다.'
}

function Test-StaleLock {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item) {
        return $true
    }
    return ((Get-Date) - $item.LastWriteTime).TotalSeconds -gt $StaleLockSeconds
}

function Wait-ForScrapeToFinish {
    param([string]$RunLockPath)

    # run_local.py 가 deploy.lock 을 보고 다음 회차를 건너뛰므로,
    # 여기서는 이미 시작된 회차가 끝날 때까지만 기다린다.
    if (-not (Test-Path -LiteralPath $RunLockPath)) {
        return
    }

    Write-Step '진행 중인 스크래핑 종료를 기다립니다.'
    $deadline = (Get-Date).AddSeconds($RunLockWaitSeconds)
    while (Test-Path -LiteralPath $RunLockPath) {
        if (Test-StaleLock -Path $RunLockPath) {
            Write-Warning '[DEPLOY] 오래된 run.lock 을 제거하고 진행합니다.'
            Remove-Item -LiteralPath $RunLockPath -Force -ErrorAction SilentlyContinue
            break
        }
        if ((Get-Date) -gt $deadline) {
            throw 'run.lock 이 제한 시간 안에 해제되지 않았습니다.'
        }
        Start-Sleep -Seconds 10
    }
}

function Invoke-RobocopyChecked {
    param([string]$From, [string]$To)

    # data 와 last_state.json 을 제외해 릴리스에 오래된 데이터 사본이 남지 않게 한다.
    # 실제 데이터는 BRE_DATA_DIR(publish clone)에서만 읽고 쓴다.
    & robocopy $From $To /E /R:2 /W:2 /NFL /NDL /NJH /NJS `
        /XD .git .venv .venv-wind-map .wind-map-work .wind-map-runtime __pycache__ releases launcher data _smoke .claude `
        /XF .env current_release.txt deployment.json last_state.json *.log *.pyc *.pyo
    if ($LASTEXITCODE -gt 7) {
        throw "소스 복사 실패 (robocopy exit $LASTEXITCODE): $From -> $To"
    }
}

function Remove-OldReleases {
    param([string]$ReleasesRoot, [string]$KeepPath, [int]$Keep)

    # @() 로 감싸지 않으면 릴리스가 1개일 때 파이프라인이 배열이 아닌 단일
    # DirectoryInfo 를 반환하고, StrictMode 에서 .Count 접근이 예외를 던진다.
    # 첫 배포에서 항상 재현되는 조건이다.
    $all = @(
        Get-ChildItem -LiteralPath $ReleasesRoot -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
    )
    if ($all.Count -le $Keep) {
        return
    }

    foreach ($stale in $all | Select-Object -Skip $Keep) {
        if ($stale.FullName -eq $KeepPath) {
            continue
        }
        Write-Step "오래된 릴리스 정리: $($stale.Name)"
        Remove-Item -LiteralPath $stale.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$resolvedSource = (Resolve-Path -LiteralPath $SourcePath).Path
$resolvedInstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)
$resolvedRuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
$releasesRoot = Join-Path $resolvedInstallRoot 'releases'
$launcherRoot = Join-Path $resolvedInstallRoot 'launcher'
$pointerPath = Join-Path $resolvedInstallRoot 'current_release.txt'
$envPath = Join-Path $resolvedRuntimeRoot '.env'
$publishPath = Join-Path $resolvedRuntimeRoot 'publish'
$deployLock = Join-Path $resolvedRuntimeRoot 'deploy.lock'
$runLock = Join-Path $resolvedRuntimeRoot 'run.lock'

New-Item -ItemType Directory -Force -Path `
    $resolvedInstallRoot, $resolvedRuntimeRoot, $releasesRoot, $launcherRoot |
    Out-Null

$previousRelease = $null
if (Test-Path -LiteralPath $pointerPath) {
    $previousRelease = (Get-Content -LiteralPath $pointerPath -Raw).Trim()
}

$pointerMoved = $false
Set-Content -LiteralPath $deployLock -Value $PID -Encoding utf8

try {
    Wait-ForScrapeToFinish -RunLockPath $runLock

    $safeSha = ($CommitSha -replace '[^a-fA-F0-9]', '').ToLower()
    if (-not $safeSha) {
        $safeSha = 'manual'
    }
    $shortSha = $safeSha.Substring(0, [Math]::Min(12, $safeSha.Length))
    $releaseName = '{0}_{1}' -f (Get-Date -Format 'yyyyMMdd_HHmmss'), $shortSha
    $releasePath = Join-Path $releasesRoot $releaseName
    New-Item -ItemType Directory -Path $releasePath | Out-Null

    Write-Step "소스 복사: $releasePath"
    Invoke-RobocopyChecked -From $resolvedSource -To $releasePath

    $python = Resolve-PythonLauncher
    $venvPath = Join-Path $releasePath '.venv'
    Write-Step '릴리스 전용 가상환경 생성'
    & $python.Executable @($python.Prefix) -m venv $venvPath
    if ($LASTEXITCODE -ne 0) {
        throw "가상환경 생성 실패 (exit $LASTEXITCODE)"
    }

    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check `
        -r (Join-Path $releasePath 'requirements.txt')
    if ($LASTEXITCODE -ne 0) {
        throw "의존성 설치 실패 (exit $LASTEXITCODE)"
    }

    # The permit atlas runs in this release environment; private data stays in RuntimeRoot.
    $windMapRequirements = Join-Path $releasePath 'wind_map\requirements.txt'
    if (Test-Path -LiteralPath $windMapRequirements) {
        & $venvPython -m pip install --disable-pip-version-check -r $windMapRequirements
        if ($LASTEXITCODE -ne 0) {
            throw "Wind map dependency installation failed (exit $LASTEXITCODE)"
        }
    }
    # 브라우저는 사용자 프로필에 캐시되어 릴리스 간 공유된다(최초 1회만 다운로드).
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw "Playwright Chromium 설치 실패 (exit $LASTEXITCODE)"
    }

    Push-Location -LiteralPath $releasePath
    try {
        if (-not $SkipTests) {
            Write-Step '단위 테스트 실행'
            & $venvPython -m unittest discover -s tests -p 'test_*.py' -v
            if ($LASTEXITCODE -ne 0) {
                throw "단위 테스트 실패 (exit $LASTEXITCODE)"
            }
        }

        if (-not $SkipSmoke) {
            if (-not (Test-Path -LiteralPath $envPath)) {
                throw "서버 환경파일이 없습니다: $envPath"
            }

            Write-Step '실제 스크래핑 스모크 실행 (격리 디렉터리, 발행 생략)'
            # --smoke 가 BRE_DATA_DIR/BRE_RUNTIME_DIR 을 <release>\_smoke 로 덮어쓰므로
            # 운영 last_state.json 과 08시 스냅샷은 오염되지 않는다.
            $env:BRE_NODE_ROLE = 'server'
            $env:BRE_ENV_FILE = $envPath
            $env:BRE_SMOKE_MIN_OK = $SmokeMinOk
            Remove-Item Env:\BRE_DATA_DIR -ErrorAction SilentlyContinue
            Remove-Item Env:\BRE_RUNTIME_DIR -ErrorAction SilentlyContinue

            & $venvPython (Join-Path $releasePath 'run_local.py') --smoke
            $smokeExit = $LASTEXITCODE
            if ($smokeExit -ne 0) {
                throw "스모크 검증 실패 (exit $smokeExit) - 포인터를 전환하지 않습니다."
            }
            Write-Step '스모크 검증 통과'
        }
    }
    finally {
        Pop-Location
    }

    $releaseMetadata = @{
        commit_sha = $CommitSha
        release_name = $releaseName
        created_at = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $releasePath 'release.json') `
        -Value $releaseMetadata -Encoding utf8

    Copy-Item `
        -LiteralPath (Join-Path $releasePath 'scripts\run_scrape.ps1') `
        -Destination (Join-Path $launcherRoot 'run_scrape.ps1') `
        -Force

    # 검증을 모두 통과한 뒤에만 포인터를 옮긴다. 실패 시 이전 릴리스가
    # 그대로 유지되므로 별도 롤백 절차가 필요하지 않다.
    Set-Content -LiteralPath $pointerPath -Value $releasePath -Encoding utf8
    $pointerMoved = $true

    $deploymentRecord = @{
        deployed_at = (Get-Date).ToUniversalTime().ToString('o')
        commit_sha = $CommitSha
        release_path = $releasePath
        previous_release = $previousRelease
        smoke_skipped = [bool]$SkipSmoke
    } | ConvertTo-Json
    Set-Content -LiteralPath (Join-Path $resolvedRuntimeRoot 'deployment.json') `
        -Value $deploymentRecord -Encoding utf8

    # 디스크 정리는 배포 성공 여부와 무관하다. 여기서 실패해도 이미 검증을
    # 통과하고 포인터까지 전환된 배포를 무효로 만들지 않는다.
    try {
        Remove-OldReleases -ReleasesRoot $releasesRoot -KeepPath $releasePath -Keep $KeepReleases
    }
    catch {
        Write-Warning "[DEPLOY] 오래된 릴리스 정리 실패(배포는 정상): $($_.Exception.Message)"
    }

    Write-Step "배포 완료: $releasePath"
    Write-Step "다음 정시 실행부터 새 릴리스가 사용됩니다. publish clone: $publishPath"
}
catch {
    if ($pointerMoved -and $previousRelease) {
        Write-Warning '[DEPLOY] 포인터를 직전 릴리스로 되돌립니다.'
        Set-Content -LiteralPath $pointerPath -Value $previousRelease -Encoding utf8
    }
    throw
}
finally {
    Remove-Item -LiteralPath $deployLock -Force -ErrorAction SilentlyContinue
}
