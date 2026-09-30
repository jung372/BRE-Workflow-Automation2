# BRE Workflow Automation

전기위원회·환경평가·한전·생태자연도 공고와 MetMast 계측기 상태를 시간당 수집해
GitHub Pages 대시보드와 MS Teams 로 알리는 모니터링 시스템입니다.

- 대시보드: <https://jung372.github.io/BRE-Workflow-Automation2/>
- 설계 문서: [docs/PRD-auto-deploy.md](docs/PRD-auto-deploy.md)

## 풍력 일간 브리핑

정적 웹의 `일간 시황` 메뉴는 오늘 보고서·날짜별 과거 호·누적 기사 검색을 제공합니다.
뉴스 서비스는 DuckDB에 발간 기사와 요약·정정 이력을 지속 보관하고, 검증한 JSON을
GitHub Pages에 게시합니다. Teams 알림은 웹 발간과 독립적으로 처리합니다.

- 아침 확인: `#/daily`, 누적 검색: `#/daily/archive`
- [실행계획](docs/PLAN-daily-wind-briefing-n8n.md) · [요구사항](docs/PRD-daily-wind-briefing.md)
- [서비스·n8n 연결 및 운영 안내](docs/wind-news-operations.md)
- 서비스 코드 `news/`, 비활성 n8n export `automation/n8n/`

요약·검증은 뉴스 전용 Codex CLI의 ChatGPT OAuth 로그인으로 자동 처리합니다(요약 Sol/low, 검증 Sol/medium). 실제 수집·Git 게시·스케줄은 서버 연결 전까지 비활성입니다.
코드 push와 별도로 서버 서비스 실행·credential 등록·n8n import가 필요합니다.
뉴스 DB 의존성은 `news/requirements.txt`의 별도 환경에 설치합니다.

## PC별 역할과 경로

| 장비 | Tailscale 이름 | 역할 | 경로 |
|---|---|---|---|
| 데스크탑 PC | `desktop-n9l7j2p` | 개발 | `C:\Personal\05 AI Study\BRE Workflow` |
| 회사 노트북 | `nb01-pf4jsbde` | 개발 (사내 프록시 사용) | `C:\Personal\05 AI Study\BRE Workflow` |
| 서버 PC | `desktop-evu6usl` | 운영·배포 대상 | `D:\05 AI Study\BRE Workflow Automation` |

개발 PC에서 `main` 에 push 하면 GitHub Actions 가 검증하고, 서버 PC의
self-hosted runner 가 자동 배포합니다. 서버에 직접 접속해 코드를 고칠 필요가
없습니다.

## 동작 구조

```
개발 PC ──push main──▶ GitHub ──test(windows-latest)──▶ deploy(self-hosted)
                                                          └ scripts\deploy_server.ps1

서버 PC 예약 작업 "BRE Scraper" (매시 06~20 KST)
  └ launcher\run_scrape.ps1 → 현재 릴리스 .venv → run_local.py
       코드: releases\<현재>\        (읽기 전용)
       데이터: RuntimeRoot\publish\  (git clone, 읽기·쓰기)
         스크래핑 → last_state.json + data\status.json → commit & push
              ├▶ GitHub Pages 갱신
              └▶ report.yml 입력 갱신

GitHub Actions "report.yml" (평일 08:30 KST)
  └ last_state.json 의 전일/금일 08시 스냅샷 비교 → Teams 발송
```

코드(릴리스 폴더), 데이터(publish clone), 비밀정보·로그(RuntimeRoot)가 분리되어
있어 배포와 롤백이 운영 데이터를 건드리지 않습니다.

## 개발 PC 최초 설치

```powershell
git clone https://github.com/jung372/BRE-Workflow-Automation2.git `
  "C:\Personal\05 AI Study\BRE Workflow"
Set-Location "C:\Personal\05 AI Study\BRE Workflow"
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

`.env` 에 MetMast 자격증명을 입력합니다. 사내망에서는 `HTTP_PROXY_KR` 도
설정합니다. `.env` 는 Git 에 올라가지 않습니다.

개발 PC는 `BRE_DATA_DIR` / `BRE_RUNTIME_DIR` 을 설정하지 않으므로 데이터와
로그가 저장소 루트에 생성됩니다.

### 교차 개발 순서

작업 시작 직전:

```powershell
git switch main
git pull --ff-only
git switch -c feature/작업이름
```

검증하고 업로드할 때:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
git add -A
git commit -m "변경 내용"
git push -u origin feature/작업이름
```

PR 을 `main` 에 병합하면 자동 배포됩니다. 같은 브랜치를 두 PC에서 동시에
수정하지 않는 것이 충돌을 줄이는 가장 단순한 원칙입니다.

> **주의.** 개발 PC에서 스크래핑을 직접 실행하면 저장소 루트의
> `last_state.json` 과 `data\status.json` 이 갱신되어 커밋 대상으로 잡힙니다.
> 개발 중에는 `--no-push` 를 쓰고, `git add -A` 전에 `git status` 로 데이터
> 파일이 섞이지 않았는지 확인하세요.

```powershell
python run_local.py --no-push     # 스크래핑만 (발행 생략)
python dashboard_app.py           # 로컬 대시보드 http://localhost:5000
```

## 서버 PC 최초 설정

운영 폴더에 저장소를 clone 한 뒤 관리자 PowerShell 에서 실행합니다.

```powershell
Set-Location "D:\05 AI Study\BRE Workflow Automation"
git pull
.\scripts\setup_server.ps1
```

첫 실행에서 `D:\05 AI Study\BRE_Workflow_runtime\.env` 가 만들어지면 실제
자격증명을 입력한 뒤 같은 명령을 다시 실행합니다. 값이 비어 있으면 계측기가
Offline 으로 오탐되므로 스크립트가 진행을 막습니다.

서버 설정은 다음을 분리합니다.

- 릴리스: `D:\05 AI Study\BRE Workflow Automation\releases`
- 현재 릴리스 포인터: `...\current_release.txt`
- 발행 전용 clone(데이터): `D:\05 AI Study\BRE_Workflow_runtime\publish`
- 환경파일·로그·락·배포 메타: `D:\05 AI Study\BRE_Workflow_runtime`

이어서 러너를 등록합니다.

```powershell
.\scripts\setup_github_runner.ps1
gh variable set SERVER_DEPLOY_ENABLED --body true
```

러너는 Windows 서비스가 아니라 서버 사용자의 로그온 세션에서 실행합니다.
그래야 예약 작업을 제어하고 `git push` 자격증명에 접근할 수 있습니다.
같은 PC의 `Stock_Report_Codex` 러너와는 루트·이름·레이블·작업명이 모두
분리되어 있습니다.

## 자동배포 동작 순서

`main` push → GitHub 호스팅 러너가 문법 검사·단위 테스트·진입점 임포트를
확인합니다. 통과하면 서버 러너가 `scripts\deploy_server.ps1` 을 실행합니다.

1. `deploy.lock` 을 잡고 진행 중인 스크래핑이 끝날 때까지 기다립니다.
2. 워크스페이스를 `releases\<시각>_<sha>` 로 복사합니다 (데이터 파일 제외).
3. 릴리스 전용 가상환경에 의존성과 Chromium 을 설치합니다.
4. 릴리스 안에서 단위 테스트를 실행합니다.
5. 격리 디렉터리(`_smoke`)로 실제 스크래핑을 1회 수행합니다.
6. **모두 통과한 뒤에만** `current_release.txt` 를 새 릴리스로 전환합니다.
7. `deployment.json` 을 기록하고 오래된 릴리스를 정리합니다(최근 5개 보존).

검증 단계에서 실패하면 포인터를 옮기지 않으므로 이전 릴리스가 그대로 유지됩니다.

`data/status.json`, `last_state.json`, `docs/**`, `**/*.md` 변경은 배포를 트리거하지
않습니다(`paths-ignore`). 서버의 데이터 push 가 배포를 되돌려 부르지 않게 하기
위한 것이며, 문서만 고친 push 도 배포되지 않습니다. 문서 수정 후 배포가 필요하면
Actions 탭에서 수동 실행하세요.

```powershell
gh workflow run ci-deploy.yml --ref main
```

> **빈 commit 으로는 파이프라인을 검증할 수 없습니다.** 변경 파일이 0개면
> `paths-ignore` 조건이 성립해 GitHub 이 실행을 건너뜁니다. 검증에는 무시
> 대상이 아닌 파일 변경이 포함되어야 하거나 수동 실행을 써야 합니다.

스모크 통과 기준은 "오류 없이 수집된 사이트 수 ≥ 3"(전체 5개)입니다. 정부
사이트(KOREC·NIE·EIASS)가 간헐적으로 타임아웃되므로 전체 성공을 요구하면 정상
코드도 배포에 실패하기 때문입니다. 저장소 변수 `BRE_SMOKE_MIN_OK` 로 조정할 수
있습니다.

## 정보공개 청구 감시 키워드 편집

"정보공개 청구" 카드는 [정보공개포털](https://www.open.go.kr/)의 **정보목록**을
사업명 키워드로 조회합니다. 키워드는 코드가 아니라 설정 데이터이므로 웹에서
직접 고칩니다.

[config/keywords.json 편집하기](https://github.com/jung372/BRE-Workflow-Automation2/edit/main/config/keywords.json)

```json
{
  "keywords": ["시루풍력", "왕신풍력", "한국바람", "해파랑육상풍력"],
  "exact_title_keywords": ["한국바람"],
  "window_days": 90,
  "row_page": 100
}
```

- **반영 시점: 다음 정시 실행(최대 1시간).** 재배포가 필요 없습니다. 서버의
  `publish` clone 이 스크래핑 직전에 `git pull` 하므로 자동으로 도달합니다.
- 포털의 단어 분리 검색을 제외하고 제목에 검색어가 공백 없이 연속으로 들어간
  건만 받으려면, 해당 검색어를 `exact_title_keywords`에도 넣습니다. 이 값은
  반드시 `keywords`에 먼저 등록되어 있어야 합니다.
- 커밋할 수 있는 사람은 저장소 write 권한 보유자뿐입니다. 권한이 없는 사용자가
  웹 편집기로 고치면 GitHub 이 자동으로 fork + PR 로 돌립니다.
- 저장 시 `키워드 설정 검증` 워크플로가 20초 내에 형식을 확인합니다. 실패하면
  GitHub 에 실패 표시가 남고, 서버는 잘못된 설정을 무시하고 기본값으로
  동작합니다(수집이 멈추지 않습니다).

### 키워드는 사업 전체명을 쓰세요

짧은 키워드는 무관한 결과가 폭증합니다. 실측값입니다.

| 키워드 | 조회 건수 | 판정 |
|---|---|---|
| 시루풍력 | 39 | 적정 |
| 시루 | 18,928 | 사용 불가 (시흥 지역화폐, 단양 시루섬 등) |
| 왕신풍력 | 63 | 적정 |
| 왕신 | 10,859 | 사용 불가 (왕신지구 수리시설 등) |

그래서 검증 규칙이 **3자 이상, 최대 20개, 중복 불가**입니다. 조회 결과가 500건을
넘으면 로그에 경고가 남습니다.

로컬에서 미리 검증할 수 있습니다.

```powershell
python keywords.py config/keywords.json
```

## 환경변수

| 변수 | 설정 위치 | 용도 |
|---|---|---|
| `METMAST_*_URL/ID/PW` | `.env` | 계측기 접속 정보 |
| `HTTP_PROXY_KR` | `.env` | 지오-차단 사이트 우회 (서버는 비움) |
| `GOTO_TIMEOUT_MS` | `.env` | 네비게이션 타임아웃 (기본 40000) |
| `BRE_DATA_DIR` | 런처 | `last_state.json` / `data\status.json` 위치 |
| `BRE_RUNTIME_DIR` | 런처 | 로그·락·배포 메타 위치 |
| `BRE_ENV_FILE` | 런처 | 환경파일 경로 |
| `BRE_NODE_ROLE` | 런처 | `server` 이면 서버 노드 |
| `BRE_SKIP_PUSH` | 런처/CLI | `1` 이면 발행 생략 |
| `BRE_SMOKE_MIN_OK` | 저장소 변수 | 스모크 통과 임계값 (기본 3) |
| `TEAMS_WEBHOOK_URL` | GitHub Secret | Teams 웹훅 (서버 `.env` 아님) |
| `SERVER_DEPLOY_ENABLED` | 저장소 변수 | `true` 일 때만 서버 배포 실행 |

## 점검

서버에서 다음 명령으로 Python·git·러너·예약 작업·포인터·발행 clone·데이터
신선도·공개 대시보드를 한 번에 확인합니다.

```powershell
.\scripts\doctor.ps1
```

수동 배포와 수동 실행:

```powershell
.\scripts\deploy_server.ps1 -SourcePath "D:\05 AI Study\BRE Workflow Automation" -CommitSha manual
Start-ScheduledTask -TaskName 'BRE Scraper'
Get-Content "D:\05 AI Study\BRE_Workflow_runtime\logs\run_local.log" -Tail 50
```

## 종료 코드 (`run_local.py`)

| 코드 | 의미 |
|---|---|
| 0 | 정상 |
| 1 | 스크래핑 예외 |
| 2 | 스모크 판정 실패 |
| 3 | 배포 진행 중이라 이번 회차 건너뜀 |

## 저장소 구조

```
config.py             사이트·계측기 정의, 프록시 설정
runtime_config.py     데이터·런타임·환경파일 경로 해석
keywords.py           정보공개 청구 키워드 로딩·검증 (CI 검증 CLI 포함)
config/keywords.json  감시 키워드 설정 (웹에서 편집)
state.py              스냅샷 상태 DB 입출력
scraper.py            스크래핑만 수행하는 최소 진입점
run_local.py          서버 예약 작업 진입점 (락·스모크·발행)
push_to_github.py     publish clone 으로 데이터 발행
notify_teams.py       Teams 보고 진입점 (GitHub Actions 에서 실행)
dashboard_app.py      로컬 개발용 Flask 대시보드
logic/                runner(수집 오케스트레이션), detector(신규 판정)
scrapers/             사이트별 파서, MetMast 체크, 재시도 유틸
presentation/         Teams 카드 빌더
tests/                단위 테스트 (외부 네트워크 비의존)
scripts/              서버 배포·세팅·점검 PowerShell
index.html, assets/   GitHub Pages 대시보드
data/status.json      대시보드 데이터 (git 추적)
last_state.json       상태 DB (git 추적 — report.yml 이 읽음)
```
