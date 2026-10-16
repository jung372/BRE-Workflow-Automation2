# 태양광 일간 시황 구현과 운영 안내

2026-10-16 기준. 태양광 전용 탭·기사 처리·게시·Teams 알림 코드와 비활성 n8n 템플릿을 제공한다. 실제 서버 설치, 뉴스 실수집, 계정의 모델 호출, Teams 수신 확인은 운영 연결 단계에서 수행한다.

## 웹과 처리 구조

웹 주소는 `#/solar`, `#/solar/dates`, `#/solar/YYYY-MM-DD`, `#/solar/archive`다. 기존 `#/daily`는 풍력 주소다. 두 화면은 같은 렌더러를 사용하며 캐시·검색·공개 데이터는 산업별로 분리한다. 최초 배포용 `data/solar-news`에는 빈 manifest만 들어 있어 실제 호가 발간되기 전에는 준비 중 화면을 보여준다.

`BRE_NEWS_INDUSTRY=solar`로 실행하면 `SOLAR_NEWS_*` 설정만 참조한다. 기사 분류·계약 단계·중복 통합·요약·근거 검증을 거쳐 태양광 전용 DuckDB에 기록하고 `data/solar-news/**`에 게시한다. DB의 산업 표식과 호 ID를 검증하여 풍력 DB 재사용을 차단한다. 공개 계약은 `schemas/solar-news-issue.json`이며 유형 필드는 `solar_type`이다.

기본 설정 파일은 `config/solar_news_collection.json`과 `config/solar_news_policy.json`이다. 실수집과 게시 기본값은 비활성이므로 설정을 검토하고 명시적으로 활성화한다. 부분 장애와 정상 0건은 구분하며, 웹 게시를 검증한 호만 발송 대상이 된다.

## 요약·검토 모델

| 역할 | 기본 모델 | 추론 강도 | 검증 |
|---|---|---|---|
| 원문에서 핵심 문장 추출·요약 | `gpt-6.1-sol` | `low` | 원문 문장·수치·기업명 일치 검사 |
| 독립 근거 검토 | `gpt-6.1-sol` | `medium` | 주체·부정 표현·조건·계약 단계와 누락 검토 |

2026-10-16 사용자 요청으로 요약·검토 모델을 모두 `gpt-6.1-sol`로 변경하여 풍력 정책 파일과 동일하게 맞췄다. 추론 강도는 요약 `low`, 검토 `medium`을 유지한다. 실제 기사에 대한 비용·속도·품질 비교와 계정의 모델 호출 검증은 아직 수행하지 않았다.

기존 Codex CLI의 ChatGPT OAuth 경로를 사용한다. 별도 API 키나 고비용 모델로 자동 전환하지 않는다. 호출 불가·사용량 한도·근거 검증 실패 시 보류하고, 계정에서 해당 모델을 사용할 수 있는지는 운영 시 확인한다. 요약 후보는 최대 15건, 하루 모델 호출은 최대 40회이며 한 기사에 요약·검토 두 번을 사용한다. 동일 근거·모델·추론 설정은 검증된 캐시를 재사용한다. 산업과 프롬프트 버전도 캐시 키에 포함한다.

태양광은 별도 인증 디렉터리를 기본으로 둔다. 동일 로그인 디렉터리를 사용하는 프로세스가 있을 때도 OS 파일 잠금으로 모델 호출과 인증 갱신을 직렬화한다. 기존 풍력 모델 설정은 별도 정책 파일에서 관리한다.

## 서버 준비

기존 n8n을 사용하며 태양광 서비스만 추가한다. 코드 release, 쓰기 가능한 publish clone, 비밀값·DB·로그 runtime을 분리한다. 실제 경로는 운영 서버에서 확정하고 아래 항목을 준비한다.

| 변수 | 용도 |
|---|---|
| `SOLAR_NEWS_RUNTIME_ROOT` | 태양광 DB·백업·로그 디렉터리 |
| `SOLAR_NEWS_PUBLISH_ROOT` | 태양광 전용 게시 clone |
| `SOLAR_NEWS_CONFIG_FILE` | collection과 policy를 포함한 비밀값 없는 JSON |
| `SOLAR_NEWS_CODEX_AUTH_ROOT` | 태양광 Codex CLI 로그인 디렉터리 |
| `SOLAR_NEWS_DOCKER_NETWORK` | 현재 n8n이 사용하는 Docker network |
| `SOLAR_NEWS_API_TOKEN` | 태양광 서비스 전용 인증 토큰, 최소 32자 |
| `SOLAR_NEWS_RELEASE` | 배포한 코드 버전 |
| `BRE_NEWS_LOCK_ROOT` | 풍력·태양광이 함께 사용하는 게시 잠금 디렉터리 |

runtime·publish·auth·공유 잠금 디렉터리는 컨테이너 UID/GID `10001:10001`이 사용할 수 있어야 한다. 설정 파일은 runtime 밖의 보호된 settings 영역에 둔다. 공개 설정 JSON은 서비스가 읽을 수 있어야 하며 토큰·웹훅은 포함하지 않는다. Compose의 기본 호스트 포트는 `127.0.0.1:8091`이다.

새 설정 파일 생성 명령은 다음과 같다. 이 명령은 기존 파일을 덮어쓰지 않는다.

```text
python scripts/solar_news/prepare_config.py --output <private-settings-directory>/config.json
```

게시 clone은 기존 사이트와 같은 저장소·브랜치를 대상으로 만들고 쓰기 권한과 Git identity를 설정한다. clone 루트에 `.solar-news-publish-clone` 표식을 만들고 `.git/info/exclude`에 등록한다. 풍력 clone의 표식·원장·인증정보를 복사하지 않는다. 복구 시에도 태양광 경로만 복원한다.

`infra/solar-news/compose.yaml`을 사용해 태양광 서비스를 설치한다. Dockerfile은 공통 `infra/wind-news/Dockerfile`을 사용하므로 선택한 release에는 `news/`, `schemas/`, 두 산업의 config, 해당 Dockerfile, 태양광 compose, `.dockerignore`, 운영 스크립트를 함께 포함한다. 태양광 이미지 build와 서비스 생성은 운영 배포 요청을 받은 단계에서 수행한다.

**두 산업의 게시를 함께 활성화하기 전에 공유 잠금을 연결한다.** 양쪽 보호된 Compose 설정에 동일한 `BRE_NEWS_LOCK_ROOT`를 지정하고 풍력 서비스에는 `infra/wind-news/shared-publish-lock.yaml`을 추가한다. 풍력 설치·Teams 재설정 스크립트는 이 값이 설정되어 있으면 overlay를 유지한다. 기존 풍력 서비스 코드도 이번 프로필·잠금 지원 버전으로 업데이트해야 한다. 태양광 publisher는 공유 잠금 설정이 없으면 게시를 거부한다.

각 clone에서 원격 브랜치 변경이 충돌하면 허용된 산업 데이터만 다시 생성해 최대 3번 push를 시도한다. 원격 공지나 다른 산업 데이터는 유지하며 강제 push를 사용하지 않는다.

## Teams 정보 입력

태양광 채팅방 이름·종류와 그 채팅방을 대상으로 하는 수신 흐름을 확인한다. 채팅방 링크를 웹훅으로 입력하지 않는다. 다음 GitHub Actions Secrets를 운영자가 직접 등록한다.

- `SOLAR_NEWS_TEAMS_WEBHOOK_URL`: 태양광 전용 수신 웹훅.
- `SOLAR_NEWS_API_TOKEN`: 설치된 태양광 서비스와 동일한 전용 API 토큰.

수신 설정 적용용 Repository Variables는 `SOLAR_NEWS_WSL_DISTRO`, `SOLAR_NEWS_WSL_USER`, `SOLAR_NEWS_RELEASE_ROOT`, `SOLAR_NEWS_SETTINGS_FILE`, `SOLAR_NEWS_TEAMS_CARD_FORMAT`이다. 경로 두 개는 서버 WSL의 절대 경로이며 settings 파일은 태양광 전용이어야 한다. 카드 형식은 수신 흐름에 맞춰 `adaptive` 또는 `messagecard`로 입력한다. `SOLAR_NEWS_SERVICE_URL`은 수동 조회용 주소로, 생략하면 `http://127.0.0.1:8091`을 사용한다.

`.github/workflows/solar-news-report.yml`에는 다음 수동 모드를 제공한다. 정기 schedule은 없다.

| 모드 | 동작 |
|---|---|
| `configure` | 전용 Secret을 보호된 stdin으로 전달하고 태양광 설정을 원자적으로 저장한 뒤 이미 설치한 태양광 컨테이너에 적용 |
| `preview` | 검증된 호의 카드 생성 가능 여부 확인. 설정 변경·발송 없음 |
| `diagnose` | 게시·설정·발송 상태 조회. 설정 변경·발송 없음 |
| `test` | 검증된 호로 ‘태양광 연결 테스트’ 발송. 일간 발송 원장과 구분 |
| `send` | 검증된 호의 실제 일간 브리핑 발송. 동일 날짜 자동 중복 차단 |

설정 파일에 풍력 변수나 공고용 웹훅 변수가 있으면 태양광 설정 도구가 거부한다. `configure`는 이미 설치된 태양광 이미지가 있어야 하며 이미지를 새로 build하지 않는다. 미리보기·상태 조회 단계에는 웹훅 Secret 자체를 전달하지 않는다. API 클라이언트는 `/health/ready`의 산업이 `solar`인지 먼저 확인한다.

GitHub Action 외에는 `scripts/solar_news/configure_teams.py --settings <private-file> --card-format <format>`을 보호된 stdin으로 실행할 수 있다. URL은 명령 인자에 넣지 않는다. 이 Python 도구 자체는 파일만 저장하며 서버 재시작이나 메시지 발송은 하지 않는다.

웹훅 누락 시 `TEAMS_NOT_CONFIGURED`를 반환하고 웹 발간을 계속한다. 풍력·공고 웹훅으로 대체하지 않는다. `test`도 게시 검증된 호가 있어야 하며 같은 호의 시험 발송을 반복하지 않는다. 시험 수신 후에는 Power Automate 실행 결과와 실제 태양광 채팅창의 표시를 확인한다. `ACCEPTED`는 웹훅 접수, `UNKNOWN`은 수신 불확실이다. 두 상태 모두 자동 재발송하지 않는다.

## n8n 연결과 운영

`automation/n8n/BRE-SOLAR-*.json` 7개를 비활성 상태로 import한다. 모든 HTTP 노드에 **BRE Solar Service** Header Auth를 연결하고 `Authorization: Bearer <태양광 API 토큰>`을 보호된 credential에 저장한다. Configure의 서비스 URL 기본값은 `http://solar-news-service:8091`이다. 기존 풍력 credential은 사용하지 않는다. Errors를 제외한 워크플로의 Error workflow를 태양광 Errors로 연결한다.

풍력 import 스크립트는 `BRE-WIND-*.json`만 읽도록 제한되어 있으므로 같은 폴더의 태양광 템플릿을 풍력 credential로 등록하지 않는다.

태양광 일정은 매시 5분·07:30 수집, 07:40 준비, 07:50 게시, 08:30~09:25 5분마다 브리핑 확인, 09:30 발송 차단, 02:30 백업이다. 모든 템플릿의 시간대는 `Asia/Seoul`이다. 시범 운영에서는 수집·웹 게시를 먼저 확인하고 시험 수신 후 Deliver를 활성화한다. 서버 기동·로그인·Docker·n8n 가용성은 기존 운영 경로에서 점검한다.

최초 서비스 배포는 `검증 및 서버 배포` workflow의 `news_action=solar-install`로 실행한다. `scripts/solar_news/install_server.ps1`과 `install_linux.sh`가 기존 WSL/n8n network에 독립 컨테이너를 설치하며, 새 전용 API 토큰을 서버 안에서 생성한다. 첫 설치의 수집·게시 설정과 모든 일정은 비활성이다. 풍력 자격증명을 복사하지 않으므로 태양광 Codex 로그인, 게시 clone과 Git 인증, 전용 Teams 웹훅, n8n credential 연결이 별도로 필요하다. 설치 결과는 값 노출 없이 연결 여부와 모델·health를 보고한다.

수집·게시 활성화는 비밀값 없는 운영 JSON의 `collection.enabled`, `policy.publisher.enabled`로 제어하고 서버 재기동 후 실제 적용을 확인한다. 재해 복구용 자동 준비·게시가 필요하면 `policy.auto_recover`도 설정한다. 하루 모델 호출 한도와 발행 소요시간을 확인해 풍력 업무가 밀리지 않게 한다.

## 검증과 복구

기본 회귀 검증:

```text
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_wind_news_frontend.js tests/test_solar_news_frontend.js
python keywords.py config/keywords.json
```

뉴스 테스트에는 `news/requirements.txt` 의존성이 필요하다. Windows의 Node 자식 프로세스 제한이 있으면 각 JavaScript 테스트 파일을 `node <file>`로 직접 실행할 수 있다. 선택적 UI 검증은 Playwright와 Edge가 있는 환경에서 `python scripts/solar_news/verify_ui.py`로 실행한다. 이 검증은 외부 요청을 차단하고 로컬 정적 화면과 가상 브리핑만 사용한다.

태양광 백업은 인증된 `/v1/backup` 작업으로 DB의 일관된 스냅샷과 해시를 생성한다. 복구 시 태양광 서비스만 중지하고 복원 대상 DB의 해시·산업 표식을 확인한다. `scripts/wind_news/restore_backup.py`의 공통 DB 복구 기능을 활용하되 태양광 전용 경로를 명시하고 재기동 후 호·manifest·검색 자료를 대조한다. 운영 DB 파일을 실행 중에 복사해 백업으로 사용하지 않는다.

문제가 생기면 태양광 n8n 일정과 발송을 비활성화하고 이전 태양광 release·설정으로 복구한다. 이미 공개된 정상 스냅샷과 발송 원장을 유지한다. 풍력 runtime·공지 데이터·기존 전달 이력은 복구 대상에 포함하지 않는다.

설계 근거: [승인된 구축 계획](PLAN-daily-solar-briefing.md). 코드 배포, 실수집, 실제 모델 응답 품질, Teams 채팅창 수신은 오프라인 fixture 검증과 별도로 확인한다.

2026-10-16 로컬 검증 결과: 뉴스 의존성이 설치된 `.venv-news`에서 Python 전체 회귀 297개, 추가 운영 클라이언트 테스트 3개, JavaScript 테스트 20개가 통과했다. Edge 헤드리스에서 초기 빈 상태, 풍력·태양광 전환, 태양광 검색·날짜별 링크·새로고침 복구·390px 모바일 레이아웃을 가상 기사로 확인했고 페이지 JavaScript 오류는 없었다. 운영 서버 배포·실제 모델 호출·Teams 실발송을 수행한 결과는 아니다.
