# 가정 Windows 서버의 n8n 구축·구동 가이드

- 작성일: 2026-09-22
- 연결 문서: [국내 풍력 일간 시황 PRD](PRD-daily-wind-briefing.md)
- 상태: 구현 단계에서 사용할 설치 절차와 자동화 산출물 명세. 현재 서버에 설치한 기록이 아니다.

## 1. 권장 설치 형태

기존 Windows 서버 PC를 계속 사용하며 뉴스용 WSL2 Ubuntu 환경에 Docker Engine·Compose를 설치한다. 그 안에서 n8n, n8n용 PostgreSQL, 뉴스 처리 서비스를 실행한다. 기사 이력용 DuckDB 파일은 뉴스 처리 서비스 한 프로세스만 연다.

```text
가정 Windows 서버 PC
├─ 기존 BRE Scraper / GitHub runner / release / publish / runtime
└─ 뉴스 전용 WSL2 Ubuntu
   ├─ Docker Engine + Compose
   │  ├─ n8n                 일정·흐름·재시도
   │  ├─ postgres            n8n 내부 상태
   │  └─ news-service        기사 처리·검토·게시·DuckDB
   ├─ /opt/bre-wind/releases/<version>     코드·Compose
   ├─ /var/lib/bre-wind/publish            뉴스 전용 Git clone
   ├─ /var/lib/bre-wind/runtime            비공개 데이터·보호된 설정
   └─ /var/backups/bre-wind                암호화 백업 생성 위치
```

WSL 환경을 쓸 수 없는 Windows 에디션·가상화 구성이라면 같은 PC 안의 Linux VM을 대안으로 검토한다. Docker Desktop이 이미 있더라도 사용자의 기존 설정·다른 컨테이너를 확인한 뒤 재사용 여부를 정한다. 별도 Docker Engine과 같은 포트·볼륨을 동시에 사용하지 않는다.

## 2. 먼저 확인할 정보

아래는 **가정 서버 PC에서** 확인한다. 개발 PC에서 나온 결과를 서버 사양으로 기록하지 않는다. 비밀번호·토큰·실제 `.env`를 읽지 않고 결과는 OS·자원·버전·기능 지원으로 제한한다.

```powershell
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, BuildNumber, OSArchitecture
Get-CimInstance Win32_ComputerSystem |
    Select-Object TotalPhysicalMemory, HypervisorPresent
Get-CimInstance Win32_Processor |
    Select-Object Name, NumberOfCores, VirtualizationFirmwareEnabled
Get-Volume | Select-Object DriveLetter, SizeRemaining, Size
wsl --status
wsl --list --verbose
```

WSL 명령이 없거나 가상화가 꺼져 있으면 해당 상태를 기록하고 설치 경로를 결정한다. 기존 예약 작업과 컨테이너는 이름·상태·포트 수준만 확인한다. 작업의 전체 실행 인자에는 비밀값이 있을 수 있으므로 무차별 덤프하지 않는다.

초기 할당 제안은 CPU 2코어 수준·여유 RAM 4GB 이상·여유 디스크 30GB 이상이다. **현재 서버가 이 조건을 충족한다는 뜻은 아니다.** LLM은 외부 API를 기본 가정하므로 로컬 GPU는 필수 요구사항이 아니다. 기존 업무와 함께 부하 측정 후 확정한다.

사용자가 결정할 항목은 재부팅 가능 시간, 원격 접근 방식, 뉴스용 Teams 채널, API 계정·예산이다. 로그인·MFA·비밀값 입력은 사용자가 직접 처리하고 이후 설정은 이어서 자동화한다.

## 3. WSL2와 Ubuntu 준비

Windows 지원 범위를 먼저 확인한다. Microsoft의 간편 설치 문서는 Windows 10의 지정 빌드 이상 또는 Windows 11을 전제로 하므로, 실제 Windows Server 제품이라면 그 제품에 맞는 절차를 다시 확인한다. [WSL 공식 설치 문서](https://learn.microsoft.com/en-us/windows/wsl/install)

관리자 PowerShell에서 배포판 목록을 확인한 뒤 승인된 배포판을 설치한다. 아래 `Ubuntu-24.04`는 선택 예시이며 목록·서버 지원 상태를 확인한 뒤 사용한다.

```powershell
wsl --list --online
wsl --install -d Ubuntu-24.04
```

기존 배포판이 있는 경우 같은 이름으로 덮어쓰거나 제거하지 않는다. 뉴스 전용 배포판 또는 기존 배포판 내 격리 환경을 정하고 이후 명령의 배포판 이름을 맞춘다. 설치가 요구하는 Windows 재시작은 정한 유지보수 시간에 수행한다.

첫 Ubuntu 실행에서 운영용 Linux 계정을 만든다. WSL 배포판을 소유하는 Windows 계정과 Linux 계정을 운영 문서에 기록하되 암호는 기록하지 않는다.

```powershell
wsl --list --verbose
wsl -d Ubuntu-24.04
```

Ubuntu 안에서 PID 1과 systemd 상태를 확인한다.

```bash
ps -p 1 -o comm=
systemctl is-system-running
```

systemd가 활성화되어 있지 않다면 `/etc/wsl.conf`의 기존 값을 보존하여 `[boot]`의 `systemd=true`를 병합한다. 변경 후 해당 배포판을 재시작하고 다시 확인한다. 모든 배포판을 종료하는 `wsl --shutdown`은 다른 업무도 멈출 수 있으므로 기본 설치 스크립트에서 호출하지 않는다. [WSL systemd 안내](https://learn.microsoft.com/en-us/windows/wsl/systemd)

## 4. Docker Engine와 Compose 설치

Ubuntu 안에서 Docker 공식 apt 저장소 등록 절차를 적용한다. 기존 Docker 설치가 있다면 호환성·사용 중인 컨테이너를 먼저 확인하고 일괄 삭제·교체하지 않는다. 공식 저장소를 등록한 다음 실행할 명령은 다음과 같다. [Docker의 Ubuntu 설치 안내](https://docs.docker.com/engine/install/ubuntu/)

```bash
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

저장소 등록을 포함한 전체 설치 절차는 구현 단계의 `install-docker.sh`로 제공한다. 해당 스크립트는 OS·아키텍처·기존 패키지·저장소 키를 확인하고 반복 실행해도 기존 설정을 손상하지 않아야 한다. Docker 권한 부여는 호스트 관리자 권한과 연결되므로 뉴스 운영 계정에 필요한 범위만 부여한다.

## 5. Compose·영속 볼륨·credential 구성

구현 단계에서 `infra/n8n/compose.yaml`, 비밀값 없는 `.env.example`, `release-manifest.json`을 제공한다. Compose는 **실행 가능한 서비스 코드와 고정된 이미지 버전이 준비된 후** 검증한다. 이 가이드만으로 현재 존재하지 않는 news-service 이미지를 실행할 수 있는 것은 아니다.

| 서비스 | 구성 요구사항 |
|---|---|
| n8n | 검증한 stable 버전·digest 고정, PostgreSQL 연결, `.n8n` 영속 볼륨, 재시작 정책 |
| postgres | 지원 버전 고정, 전용 DB·계정·비밀값, 영속 볼륨, readiness 확인 |
| news-service | 고정 코드 이미지, 뉴스 runtime·publish 볼륨, 내부 HTTP API, 단일 DuckDB writer |
| 선택 HTTPS proxy | 운영자 private 진입점. 뉴스 API·DB 포트는 외부 노출하지 않음 |

볼륨은 WSL Linux 파일시스템에 생성한다. PostgreSQL 메이저 버전에 맞는 `PGDATA`·볼륨 경로를 확인하고 데이터가 실제 볼륨에 기록되는지 재시작으로 검증한다. n8n 공식 Compose 안내와 PostgreSQL 예제를 참고하되 Assistant용 sandbox 전체를 자동 포함하지 않는다. [n8n Compose 안내](https://docs.n8n.io/deploy/host-n8n/install-options/install-using-docker-compose.md)

필수 설정은 다음과 같다. 값은 예시 또는 변수명이며 실제 비밀값을 포함하지 않는다.

| 설정 | 적용 |
|---|---|
| `TZ`, `GENERIC_TIMEZONE` | `Asia/Seoul` |
| `DB_TYPE` | `postgresdb` |
| `DB_POSTGRESDB_HOST` 등 | Compose 내부 PostgreSQL 서비스·DB·계정 |
| `N8N_ENCRYPTION_KEY` | 최초 한 번 안전하게 생성, 출력하지 않고 보호된 파일에 기록, 별도 암호화 백업 |
| n8n 편집기 URL·proxy 설정 | 실제 HTTPS 경로에 맞춤. proxy hop 수는 실제 구조에 맞춰 설정 |
| `EXECUTIONS_DATA_SAVE_ON_SUCCESS` | `none` 제안. 운영 지표는 별도 최소 로그 |
| `EXECUTIONS_DATA_SAVE_ON_ERROR` | 운영 기본 `none` 제안. 오류 메타데이터만 별도 저장 |
| `EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS` | `false` 제안. 개발 중 필요할 때만 마스킹된 표본 저장 |
| `EXECUTIONS_DATA_PRUNE` | `true` |
| `EXECUTIONS_DATA_MAX_AGE` | `168`시간 제안. 저장 정책·목적에 맞춰 확정 |
| `EXECUTIONS_TIMEOUT` | `1200`초 제안. 개별 workflow의 더 짧은 시간 제한 우선 |

실행 데이터 저장·보존·timeout 변수는 선택한 n8n 버전에서 검증한다. [n8n 실행 설정](https://docs.n8n.io/deploy/host-n8n/configure-n8n/basic-configuration/use-environment-variables/executions.md)

PostgreSQL 사용 시에도 n8n의 `.n8n` 영속 저장과 암호화키 관리가 필요하다. [n8n Docker·영속 저장 설명](https://docs.n8n.io/deploy/host-n8n/install-options/install-with-docker.md)

보호된 변수 파일은 소유자 전용 권한으로 보관한다. 비밀값을 확인하기 위해 `cat .env`, 전체 `docker inspect`, 전체 설정 덤프를 사용하지 않는다. Compose 검증도 값이 출력되지 않는 `config --quiet`를 사용한다. workflow export에 inline Authorization 헤더나 webhook URL을 저장하지 않는다.

## 6. 최초 구동과 n8n 기본 설정

아래 명령은 구현된 릴리스의 Compose 경로와 보호된 변수 파일이 준비된 후 Ubuntu 안에서 실행한다. `{release}`와 `{protected-env}`는 실제 경로로 치환할 자리다.

```bash
sudo docker compose --project-name bre-wind --env-file {protected-env} -f {release}/compose.yaml config --quiet
sudo docker compose --project-name bre-wind --env-file {protected-env} -f {release}/compose.yaml pull
sudo docker compose --project-name bre-wind --env-file {protected-env} -f {release}/compose.yaml up -d
sudo docker compose --project-name bre-wind --env-file {protected-env} -f {release}/compose.yaml ps
```

1. PostgreSQL readiness와 n8n `/healthz`를 확인한다. 이어서 n8n 로그인과 테스트 workflow 실행으로 DB 연결까지 확인한다.
2. news-service의 인증된 readiness를 확인한다. DB 접근·단일 writer·필수 설정 상태를 확인하되 실제 기사 수집은 시작하지 않는다.
3. 최초 접속은 가정 서버 자체의 localhost 또는 승인된 private 접속 경로를 사용한다. Windows localhost 전달은 실제 WSL 네트워크 모드에서 검증한다. [WSL 네트워킹](https://learn.microsoft.com/en-us/windows/wsl/networking)
4. n8n owner 계정을 만들고 사용 가능한 2단계 인증을 설정한다. 정기 운영은 trusted HTTPS와 private 접근을 구성한다.
5. 서버·workflow timezone을 모두 `Asia/Seoul`로 설정한다.
6. n8n에서 뉴스 API, LLM, Teams, news-service 연결용 Credentials를 등록한다. 필요한 서비스에만 해당 credential을 연결한다.
7. 모든 workflow를 비활성 상태로 import한 뒤 한 개씩 수동 검증한다. export된 credential ID는 현재 인스턴스의 실제 credential과 다시 연결한다.

초기 접근에서만 필요한 예외와 운영 설정을 구분한다. secure cookie 문제를 해결하기 위해 운영 전체의 보안 옵션을 무조건 끄지 않는다. DB·뉴스 내부 API·Docker 소켓을 인터넷에 공개하지 않는다.

## 7. 기사 수집·요약·검토 연결

1. 네이버 개발자 계정에서 검색 API 앱을 준비하고 Credentials에 키를 직접 입력한다. 비밀값을 표시하지 않는 테스트로 연결을 확인한다.
2. 초기 국내 출처 목록과 키워드를 등록한다. 첫 샘플은 소량으로 제한하고 기사시각·원문 URL·중복 기사·단계 분류를 직접 대조한다.
3. LLM 공급자·모델·일/월 상한을 설정한다. 문서에 예산이 미정이면 유료 자동 실행은 비활성으로 유지한다.
4. HTTP Request·RSS·분기 노드를 처리 서비스에 연결한다. 서비스는 batch ID와 job ID를 반환하고 n8n이 결과를 조회한다.
5. fixture로 같은 사건의 5개 기사, MOU와 본계약, PF 약정과 종결, 원문 없는 기사, 기사 0건, 출처 timeout을 재생한다.
6. 내부 검토 화면에서 대표기사 변경, 사건 분리·병합, 보류·승인, 변경 후 재승인을 검증한다.
7. 최초 7일은 실제 수집·처리 결과를 비공개로 비교한다. 자동 게시 전 PRD의 품질 목표를 확인한다.

## 8. GitHub Pages와 Teams 연결

### 8.1 웹 게시

뉴스 전용 publish clone과 Git 권한을 준비하고 수정 가능 경로를 `data/wind-news/**`로 제한한다. Git 인증 자체가 파일 경로 단위 권한을 제공한다고 가정하지 않고 publisher의 allowlist·검증으로 제한한다. GitHub Pages 설정의 source·branch·폴더를 확인하여 게시 방식과 인증 수단을 정한다.

격리 Git 원격으로 공지·뉴스 동시 push를 검증한 다음 staging으로 확인한다. 실제 운영 게시에서는 `git push` 성공과 Pages URL의 호 ID·revision·해시 확인을 별도 기록한다. 뉴스 JSON 변경이 기존 Windows 코드 배포를 유발하지 않는지도 확인한다.

### 8.2 Teams 수신 설정

1. 사용자가 원하는 테스트 채널에서 `Workflows`를 연다.
2. webhook으로 채널 알림을 보내는 템플릿 또는 `When a Teams webhook request is received` 트리거를 선택한다.
3. 대상 채널·허용 호출자·인증 방식을 테넌트 정책에 맞게 설정한다. 사용자 한 명 퇴사·계정 변경으로 멈추지 않도록 공동 소유자를 지정한다.
4. 생성한 뉴스용 URL·인증정보를 n8n Credentials에 직접 입력한다. 채팅·Git·스크린샷에 표시하지 않는다.
5. `[테스트] 풍력 일간 시황` 카드 한 건으로 실제 채널 수신을 확인한다. payload 형식과 카드 렌더링을 실제 workflow에 맞춘다.
6. 요청 2xx, workflow 실행 성공, 실제 메시지 확인을 구분해 기록한다. timeout 시 전송 원장이 `UNKNOWN`으로 남는지 검증한다.
7. 웹에서 확인한 일간 호 링크만 정상 뉴스 카드에 포함한다. 운영 채널 전환 후 최초 1회 메시지 확인까지 완료한다.

이는 신규 뉴스 연계 절차이며 기존 공지의 webhook 변경을 요구하지 않는다. UI·인증 방식은 테넌트 정책에 따라 달라질 수 있다. [Microsoft 공식 Teams 안내](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook)

## 9. Windows 재부팅·로그아웃 대응

서버 예약 작업을 만드는 시점에는 뉴스 인프라 구현·운영 설정 작업으로 범위를 확정한다. 새로운 작업명은 `BRE Wind n8n Start`와 `BRE Wind n8n Watchdog`를 제안한다. 기존 `BRE Scraper`와 GitHub runner 작업은 수정하지 않는다.

| 항목 | 요구사항 |
|---|---|
| 실행 계정 | 해당 WSL 배포판 소유 Windows 계정. SYSTEM 계정에서 같은 배포판이 보인다고 가정하지 않음 |
| 시작 | Windows 시작 후 네트워크 준비를 기다리고 실행 |
| 로그온 | 사용자 로그인 여부와 관계없는 실행 구성. 저장 credential·정책 요구를 실제 계정으로 확인 |
| 실행 방식 | 창이 표시되지 않는 작업, stdout·stderr는 민감값을 제거해 제한된 로그로 저장 |
| 중복 방지 | 이미 실행 중인 시작 작업은 새 인스턴스를 만들지 않음 |
| 종료 정책 | 장기 실행 supervisor가 임의의 기본 실행 시간 제한으로 종료되지 않게 설정 |
| 감시 | 5분마다 WSL·Docker·n8n·news-service·최근 성공 호 점검 |

시작 스크립트는 `wsl.exe -d <뉴스 배포판> --exec /bin/bash <supervisor 경로>` 형태로 지정 배포판을 명시한다. supervisor는 Docker 준비를 기다리고 고정된 Compose를 실행한 뒤 지속 상태를 감시한다. WSL 프로세스 수명과 로그아웃 동작은 실제 Windows 버전에서 검증한다. systemd·컨테이너 restart 옵션만 설정한 상태를 무인 운영 완료로 간주하지 않는다.

검증 순서는 컨테이너 재시작 → 지정 WSL 배포판 재시작 → Windows 재부팅 → 로그인 없이 health 확인 → 로그아웃 상태 24시간 수집이다. 기존 공지 스크래핑의 다음 실행도 정상인지 함께 확인한다.

무인 기동이 반복해서 실패하면 같은 PC의 자동 시작 가능한 Linux VM을 대안으로 선정한다. 이 경우 데이터·workflow·credential 암호화키를 포함해 검증된 복구본으로 이전하고 WSL 운영 인스턴스의 스케줄을 중지하여 이중 발송을 막는다.

## 10. 백업·복구·업데이트

### 10.1 백업

- PostgreSQL은 해당 버전에 맞는 논리 백업을 사용한다.
- DuckDB는 writer를 잠시 멈추고 checkpoint·일관성 확인 후 복사한다. 쓰는 중인 파일 하나를 임의 복사하지 않는다.
- n8n 암호화키, 보호된 설정, workflow export, 코드·이미지 버전, 공개 호 manifest를 함께 관리한다. 비밀 파일은 암호화하고 접근자를 제한한다.
- 7개 일간·4개 주간 백업을 초기 보존안으로 두고 다른 물리 장치 또는 승인된 저장소에 복제한다.
- 복사한 백업의 checksum·일자·크기를 확인한다. 실제 복구시험을 통과한 백업만 복구 가능 상태로 표시한다.

### 10.2 복구

1. 격리 환경에 동일 버전 컨테이너와 볼륨을 준비한다.
2. n8n DB·암호화키·뉴스 DB·설정을 복구하되 모든 수집·게시·Teams workflow는 비활성 상태로 둔다.
3. 키를 출력하지 않고 credential 연결과 호 생성 결과를 확인한다.
4. 공개된 호·Git commit·전달 원장을 대조하여 이미 게시·발송된 항목의 재처리를 차단한다.
5. 단일 운영 인스턴스만 활성화하고 최신 호 1개에 대한 다음 실행을 확인한다.

### 10.3 업데이트·롤백

릴리스에는 n8n·PostgreSQL·뉴스 코드·schema·workflow 버전을 기록한다. staging에서 백업 복원과 다음 호 생성을 확인한 후 고정 버전을 변경한다. DB 마이그레이션이 수행된 경우 이미지 태그만 이전으로 바꾸는 것으로 복구된다고 가정하지 않고, 호환되는 DB·키·설정 백업을 함께 복원한다. 자동 `latest` 업데이트와 볼륨 삭제를 운영 기본 절차로 사용하지 않는다.

## 11. 제공할 자동화 산출물

아래 파일은 구현 단계에 제작할 목록이다. 이번 문서 작성에서 생성·실행한 스크립트가 아니다.

| 파일 | 역할 |
|---|---|
| `scripts/wind_news/preflight.ps1` | 서버 OS·자원·WSL·포트·충돌 확인, 비밀값 없는 보고서 |
| `scripts/wind_news/setup-wsl.ps1` | 배포판 확인·설치 보조, 재실행 가능, 기존 배포판 보존 |
| `scripts/wind_news/install-docker.sh` | 공식 apt 저장소와 Docker Engine·Compose 설치·검증 |
| `scripts/wind_news/deploy.ps1` | 검증된 release 전송·Compose 배포·health·롤백 |
| `scripts/wind_news/register-tasks.ps1` | 뉴스 전용 시작·감시 작업만 등록 |
| `scripts/wind_news/start-and-supervise.sh` | WSL 내 Docker·Compose 시작과 지속 감시 |
| `scripts/wind_news/backup.sh`, `restore.sh` | 백업·격리 복구·checksum·버전 확인 |
| `automation/n8n/WF-01`~`WF-06` JSON | PRD의 수집·준비·게시·전달·복구·백업 흐름 |
| `infra/n8n/compose.yaml`, `.env.example` | 선택 버전·영속 볼륨·private 네트워크·변수명 |
| `docs/wind-news-operations.md` | 실제 설치 버전·비밀 없는 경로·운영·장애 대응 |

## 12. 구축 완료로 판단할 증거

다음 증거가 모두 있어야 실제 구축 완료로 보고한다.

1. 가정 Windows 서버에서 n8n 로그인·DB 연결·뉴스 처리 workflow 실행 확인.
2. 국내 기사 실표본의 사건별 대표기사·요약·단계 분류 검증.
3. 기존 사이트의 좌측 메뉴·기존 공지·일간 시황·과거 호 동작 확인.
4. GitHub의 실제 게시 JSON과 사이트가 같은 호·revision을 보여주는 증거.
5. 지정 Teams 채널의 실제 뉴스 카드와 날짜별 웹 링크 확인.
6. 같은 실행을 재시도해도 중복 게시·확인된 중복 발송이 없고, 모호한 수신 상태는 원장에 남는 증거.
7. 재부팅·로그아웃 후 자동 구동, 기존 BRE 작업 정상 동작, 백업 복구 결과.
8. 운영 계정·비밀값을 제외한 설치 버전, 비용 상한, 점검·복구 방법 인계.

현재는 설계와 절차를 작성한 단계다. 실제 Windows 에디션, WSL 상태, 서버 접근 가능 여부, API credential, 뉴스 품질, 운영 게시·Teams 수신은 구현 시 확인할 항목으로 남아 있다.
