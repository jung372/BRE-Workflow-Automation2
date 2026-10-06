# 풍력 일간 브리핑 구현·운영 안내

구현 대상 저장소는 <https://github.com/jung372/BRE-Workflow-Automation2>다. 공지 수집과 분리한 Python 뉴스 서비스가 DB와 발간 이력을 관리하고, 기존 n8n이 작업을 호출한다. 공개 웹은 `#/daily`의 오늘 브리핑과 `#/daily/archive`의 누적 기사 검색을 제공한다.

## 2026-10-06 동일 협약 보도 통합 조정

로컬 구현: `news/dedup.py`의 `agreement-evidence-v2`는 요약 후보 선정 **전에** 동일 사건을 통합한다. 기존 정확 일치 규칙에 더해, 본문 앞부분의 실제 체결 문장에서 따옴표로 표시된 협약명·당사자 2곳·체결일을 추출한다. 회사명 약칭/법인 표기와 `지난 2일`/`10월 2일`을 정규화하고, 협약 목적의 충분한 문자 중복을 확인한다. 기사 발행일을 체결일로 대신 쓰지 않는다.

- 같은 당사자·체결일·육상/해상 구분이 일치하고 협약 목적이 서로 뒷받침될 때만 통합한다. 서로 명시된 지역·프로젝트·숫자가 다르면 분리한다. 그룹의 모든 구성원과 비교하므로 중간 기사 하나로 다른 사건을 연쇄 통합하지 않는다.
- 본계약·금융종결·해지·사고를 다루는 제목은 협약 배경 문장으로 통합하지 않는다. 체결 예정·부정·취소, 날짜/본문 부족, 복수 체결 사건도 통합 근거로 사용하지 않는다. 협약 외 사건은 기존 정확 일치 규칙을 유지한다.
- 대표기사 선정 기준은 매체 신뢰도 → 본문/설명 충실도 → 기사 시각/URL이다. 원 기사 전부와 사건 구성원 관계는 DB에 남기며 요약은 대표기사 근거만 사용한다. 원문 표현이 다른 기사들의 사실을 합성하지 않는다.
- 사용자 제시 4건은 10월 2일 서부발전·전남개발공사 육상풍력 업무협약 1건으로 통합되는 저장 표본 테스트를 추가했다. 세 원문은 공개 접근으로 확인했고, 브레이크뉴스는 403 응답으로 기존 게시 요약의 체결 문장을 표본에 사용했다.

이 조정은 로컬 코드 검증이며 운영 서버 설치·오늘 호 정정 발간은 별도 작업이다. 이미 발간된 immutable issue JSON은 자동으로 덮어쓰지 않는다.

배포·정정은 `workflow_dispatch`의 `news_action=dedup-correct`로 실행한다. 전체 CI 후 뉴스 컨테이너만 설치하고, 정정 전 API 백업의 해시를 확인한다. 오늘 호의 실제 발간된 승인 구성원과 원래의 frozen batch만 재사용하며 신규 수집·요약 보류 강제 승인·Teams 발송을 하지 않는다. 기존 호는 보존하고 새 revision을 발간한다. 이미 발간 목록 밖에 있던 승인 후보도 이번 정정으로 새로 노출하지 않는다. 운영 스크립트는 그룹 수 감소·모든 기존 구성원 보존·모든 요약 검증을 확인한 뒤 게시하고 `WEB_VERIFIED`를 요구한다.

## 2026-10-01 산업 전반 수집 확장

실제 서버 검증 완료: 확대 수집 회차에서 **113개 매체 184건**을 DB에 적재했고, 2026-10-01 보고서 revision 3을 **12개 매체 12건**으로 재발간했다(16:11 KST). 발간 스냅샷은 수집 188건·적합 106건·중복 통합 1건·검증 보류 8건·요약 대상 외 85건이다. 21:30 KST의 후속 검증에서 DB 누적 **262건**, 최근 자동 수집 시각 **21:06 KST**, 검색어 24개 모두 오류 없이 검색 완료 또는 페이지 상한 도달(9개/15개)을 확인했다. 원문 파싱 실패와 상한 때문에 수집 상태는 여전히 `partial`로 정직하게 표시한다.

근거: [전체 테스트 및 설치·수집·재발간](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36827600611), [main CI 및 기존 서버 배포](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36827986993), [후속 DB 백업·OAuth·검색 진단](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36862118263). Python 220개·웹 16개 테스트 통과. 뉴스 컨테이너 코드 릴리스 `7deaf02cc47707a8c63cb53b48b8ca70befee315`. 백업 해시, 컨테이너 healthy, ChatGPT OAuth 두 단계 검증, Git 게시 연결 확인. 기존 n8n 일정 5개는 유지하며 Teams는 여전히 미연결이다.

운영 수집은 3개 매체의 목록에 더해 네이버 공개 뉴스 검색으로 산업·공급망·기업·프로젝트 관련 24개 검색어를 순회한다. 매체를 3곳으로 제한하지 않고 검색에서 발견한 원문을 읽는다. 검색 요약문은 AI 근거에 포함하지 않으며, 언론사 본문과 발행시각이 확인된 기사만 DB에 넣는다. 구글은 이번 HTTP 시험에서 기사 목록을 반환하지 않아 운영 수집 경로에 포함하지 않았다.

- 검색별 최대 3페이지, 회차별 원문 최대 240건. 상한·실패가 있으면 일부 수집으로 기록한다.
- 다음 페이지는 검색 화면이 제공하는 주소를 따른다. `/p/newssearch/3/api/tab/more`와 `/4/` 등 숫자 버전을 허용하되 호스트·경로 패턴은 검증한다. 버전 하나로 고정하면 응답별로 검색이 누락되는 회귀를 테스트로 방지한다.
- 수집 기사 전체를 DB에 보존하고, 하루 최대 15개 발간 항목을 선정한다. 산업 관련 제목·최신성·매체와 제목 다양성을 요약 후보 선정에 반영한다. 비슷한 제목은 뒤로 미루며 확인되지 않은 사건을 임의로 병합하지 않는다.
- 요약 예산으로 처리하지 않은 기사는 `deferred` / ‘이번 호 요약 대상 외’로 표시해 `held` / ‘검증 보류’와 구분한다.
- `verify`는 최근 배치의 검색별 페이지·발견 건수·상태, 매체별 원문 처리 결과, 서버 시각과 검색어 수를 출력한다. 비밀값·원문 본문·검색 요약문은 로그에 출력하지 않는다.
- `refresh` 운영 작업은 전체 CI 통과 → 뉴스 컨테이너 설치 → 운영 검색 설정 갱신 → 수집 → 최신 발간 호 정정 → Pages 해시 검증 순서로 실행한다. n8n 일정과 OAuth 인증정보를 다시 설정하지 않는다.

수집 설정: `config/wind_news_collection.json`. 파서: `news/search_sources.py`, `news/public_sources.py`. 산업 범위·요약 후보 우선순위: `news/relevance.py`.

## 1. 코드 제공과 실제 운영의 구분

2026-09-30 후속 사용자 지시: 계약·견적 문의를 발간 선행 조건에서 제외하고 `public_publishers` 수집기를 사용한다. 외부 유료/무료 식별값 없이 원문의 일반 공개 접근과 본문 추출을 확인하며, 로그인·결제 요구 기사는 제외한다. `require_rights_review=false`는 사용자 운영 방침이며 출처별 검토 완료로 표시하지 않는다. 출처·원문 링크와 접근 근거·확인 시각을 기록한다. 아래 과거의 대체 공급원 확보 대기 상태는 이 지시로 해제한다.

체크인 기본값은 **Codex OAuth 요약·자동 검증 방식**이며 수집·Git 게시·운영 스케줄은 서버 연결 전까지 꺼져 있다. 공개 `data/wind-news`에는 빈 manifest만 둔다. 가상 기사를 실제 날짜의 뉴스처럼 게시하지 않는다. n8n export도 모두 비활성 상태다.

실제 뉴스가 쌓이려면 서버에 뉴스 서비스를 실행하고 뉴스 API·출처 정책·전용 publish clone을 연결해야 한다. 코드 push만으로 기존 n8n에 workflow가 자동 설치되지는 않는다. 이 구성은 기존 n8n·DB·Windows 예약 작업·BRE Scraper를 교체하지 않는다.

Teams가 미설정이거나 실패해도 웹 발간과 검색은 독립적으로 완료한다. Teams가 준비되기 전에는 웹으로 아침 브리핑을 확인할 수 있다. 기본 공개 주소는 `https://jung372.github.io/BRE-Workflow-Automation2/#/daily`이며 Pages 반영 검증 후 실제 이용 가능 여부를 판단한다.

## 2. 뉴스 전용 실행 환경

Windows 네이티브 설치와 기존 n8n Docker 환경에 서비스만 추가하는 구성 중 실제 서버에 맞는 것을 선택한다. 개발 PC에서 실행한 결과를 서버 설치 기록으로 해석하지 않는다.

Windows에서 코드 릴리스 디렉터리 기준:

```powershell
python -m venv .venv-news
.\.venv-news\Scripts\python.exe -m pip install -r news\requirements.txt
.\.venv-news\Scripts\python.exe -m news
```

마지막 명령 전 서비스 계정의 보호된 환경에 다음 변수를 등록한다. 실제 값은 채팅·Git·로그에 넣지 않는다. 뉴스 서비스는 기존 scraper의 `.env`를 읽지 않는다.

| 변수 | 역할 |
|---|---|
| `WIND_NEWS_RUNTIME_DIR` | 저장소 밖 뉴스 전용 runtime 디렉터리. DB·백업·작업 상태 저장 |
| `WIND_NEWS_API_TOKEN` | 최소 32자 뉴스 전용 인증 토큰. n8n Header Auth와 일치 |
| `WIND_NEWS_CONFIG` | 선택: `{ "collection": {...}, "policy": {...} }` 형식 운영 JSON |
| `WIND_NEWS_HOST`, `WIND_NEWS_PORT` | 기본 `127.0.0.1`, `8090`. 내부 도달 경로에 맞게 선택 |
| `WIND_NEWS_PUBLISH_CLONE` | 뉴스 전용 게시 clone 경로 |
| `WIND_NEWS_NAVER_CLIENT_ID`, `WIND_NEWS_NAVER_CLIENT_SECRET` | 폐기된 네이버 경로의 과거 변수. 새로 발급하거나 입력하지 않음 |
| `WIND_NEWS_TEAMS_WEBHOOK_URL` | 선택: 뉴스용 Teams Workflows 수신 주소 |
| `WIND_NEWS_CODEX_HOME` | 저장소 밖 뉴스 전용 Codex OAuth 로그인 디렉터리. CLI가 토큰 갱신을 관리 |
| `WIND_NEWS_CODEX_BINARY` | 선택: 공식 Codex CLI 실행 파일 경로. 기본 `codex` |

기존 MetMast·프록시·공지 Teams credential을 뉴스 서비스에 공유하지 않는다. Docker에서는 Windows 자격증명 관리자가 자동 제공되지 않으므로 Git 인증도 서비스 계정에 맞게 별도로 연결한다.

### Docker로 뉴스 서비스만 추가할 때

`infra/wind-news/compose.yaml`은 기존 n8n의 Docker network 이름을 명시해야 실행된다. `.env.example`의 변수명을 참고하여 보호된 환경파일을 저장소 밖에 작성한다. runtime·publish 디렉터리는 컨테이너 사용자 UID/GID 10001이 사용할 수 있어야 한다. 운영 시 베이스 Python 이미지는 검토한 digest로 고정한다.

```text
docker compose --env-file <보호된-환경파일> -f infra/wind-news/compose.yaml config --quiet
docker compose --env-file <보호된-환경파일> -f infra/wind-news/compose.yaml build
docker compose --env-file <보호된-환경파일> -f infra/wind-news/compose.yaml up -d
```

이는 설치·갱신 시 사용하는 명령이다. 실제 서버 설치 이력은 아래 9절을 참고한다. `config`의 전체 출력이나 전체 `docker inspect`는 비밀값을 노출할 수 있으므로 사용하지 않는다. 기존 n8n이 Docker가 아니면 이 구성을 억지로 적용하지 않는다. 컨테이너 간 `http://news-service:8090`과 Windows `localhost`는 서로 다른 주소다.

## 3. 출처·요약·승인 정책

`config/wind_news_collection.json`과 `config/wind_news_policy.json`이 기본 정책이다. 운영 JSON의 `collection`, `policy`에 필요한 값을 지정한다. 후보 출처의 실제 도메인·이용 조건을 검토하고 `rights_reviewed`를 기록한 후 수집을 활성화한다. 후보 목록은 수집 권한이 확인된 목록을 뜻하지 않는다.

**2026-09-30 수집 경로 정정:** 네이버 검색 API의 현행 특약 2.3은 AI 입력과 저장·파생물 이용을 제한하고, 2.4의 서버 이력 캐시는 최대 21일이다. 따라서 영구 뉴스 DB와 OAuth 자동 요약에 이 경로를 사용하지 않는다. 실제 네트워크 수집은 설정을 켜더라도 `NAVER_TERMS_INCOMPATIBLE`로 차단한다. 기존 파서는 오프라인 fixture 회귀용으로만 남긴다. [확인한 공식 약관](https://developers.naver.com/products/terms/).

대체 수집원은 원 제공자가 텍스트의 저장·AI 처리·요약 게시를 허용한 API/피드 또는 공공누리 등 이용허락 표시를 확인한 공공자료로 한정한다. RSS 제공만으로 AI·영구저장 권한이 있다고 간주하지 않는다. 아직 대체 수집원은 운영 승인·구현되지 않았으며 일반 언론 뉴스 수집 완료로 표시하지 않는다. 필수 발견원/출처 실패와 정상 0건을 구분한다.

사용자는 일반 언론 기사 포함을 우선하고 유료 기사는 제외하도록 확정했다. `policy.require_free_access=true`로 무료 공개 여부가 확인된 `access_status=free` 기사만 DB 적재·요약·발간 후보에 포함한다. 유료 또는 확인되지 않은 기사는 본문 저장이나 LLM 호출 전에 제외한다. 기존 배치도 준비 단계에서 같은 조건으로 거른다. 각 수집 어댑터는 원문의 무료 열람 근거를 확인한 뒤 상태를 전달해야 하며 검색 미리보기나 제목만으로 무료 판정을 해서는 안 된다. 무료 열람 확인은 저작권 이용 검토를 대체하지 않는다. 공급 API 요금은 기사 유료 여부와 별도로 검토하고, 승인 전 결제·계약을 하지 않는다. [수집 서비스 검토](wind-news-source-review.md).

자동 수집 재조회 범위는 기본 최근 72시간이다. 그보다 긴 서버 중단 이력은 자동 복원이 보장되지 않으며, 확보된 과거 자료를 인증된 ingest API로 적재하고 명시적인 `batch_ids`로 과거 날짜 호를 준비한다. 네이버 검색의 페이지·쿼터 한도에 도달하면 완전한 수집으로 간주하지 않는다.

2026-09-30 최종 사용자 지시에 따라 **수동 검토를 일간 발간의 필수 단계로 두지 않고 OpenAI OAuth로 자동 요약·근거 검증**한다. 서버의 공식 Codex CLI를 ChatGPT 계정으로 로그인하고 서비스는 `codex exec`를 호출한다. 요약은 `gpt-6.1-sol` / `low`, 독립 검증은 `gpt-6.1-sol` / `medium`이 기본값이다. 계정에서 사용 가능한 모델은 운영 전 확인하고 policy의 `model`, `review_model`로 조정한다.

요약은 주어진 근거의 완전한 문장 발췌만 허용한다. 코드가 인용·기업·프로젝트·지역의 실제 근거 포함을 확인하고 두 번째 모델 호출이 주체·계약 단계·부정·조건을 검증한다. 통과 항목은 자동 승인, 실패·한도 초과·인증 만료 항목은 보류한다. 일일 호출 예약 한도는 기본 120회(기사당 요약+검증 2회)이며 재시작해도 초기화되지 않는다. 동일 근거·모델의 검증된 응답은 캐시하고 인용이 포함된 캐시는 30일 내 만료한다. 기사 없음과 검증 실패로 미선정인 상태를 구분한다.

이는 별도 Platform API 키 과금 경로를 사용하지 않으며 ChatGPT/Codex 구독 한도가 적용된다. API 키 로그인으로 자동 전환하지 않는다. OAuth 토큰을 코드·n8n credential·GitHub Actions에 넣거나 직접 refresh endpoint로 갱신하지 않는다. 공식 CLI가 전용 인증 디렉터리에서 로그인과 갱신을 관리한다. 원문 전체를 읽지 않은 기사는 계속 `제목·검색 요약 기준`으로 표시한다.

### OAuth 최초 연결

서버의 뉴스 서비스 계정으로 공식 Codex CLI를 설치하고 **뉴스 전용 `CODEX_HOME`을 지정한 로그인 프로세스에서** `codex login` 또는 `codex login --device-auth`를 실행한다. 브라우저 로그인·MFA·기기 코드는 사용자가 완료한다. 이 디렉터리를 서비스의 `WIND_NEWS_CODEX_HOME`과 동일하게 지정하고 `codex login status`가 ChatGPT 로그인임을 확인한다. 개발 PC의 현재 로그인·토큰을 복사하거나 출력하는 작업은 수행하지 않는다.

CLI는 빈 작업 디렉터리, 읽기 전용 sandbox, shell·웹 검색·앱·하위 에이전트 비활성 상태로 실행한다. 서비스 API 키·뉴스 API 비밀키·Teams webhook은 CLI 자식 프로세스에 전달하지 않는다. Docker 구성은 공식 `@openai/codex` 0.159.2를 설치하고 `WIND_NEWS_CODEX_AUTH_ROOT`를 별도 영속 볼륨으로 연결한다. 갱신을 위해 서비스 계정 쓰기 권한이 필요하다. 하나의 인증 디렉터리는 이 직렬 서비스만 사용한다.

공식 근거: [Codex 인증](https://learn.chatgpt.com/docs/auth), [비공개 자동화에서 계정 인증 유지](https://learn.chatgpt.com/docs/auth/ci-cd-auth), [비대화형 실행](https://learn.chatgpt.com/docs/developer-commands#codex-exec). 공식 안내도 일반 자동화에는 API 키를 권장하며 계정 인증 유지 방식은 신뢰할 수 있는 비공개 실행 환경용이다. 여기서는 사용자가 지정한 OAuth 방식으로 서버 내부에서만 실행하며 공개 GitHub CI에는 인증 파일을 연결하지 않는다.

선택적 정정·예외 처리 화면은 뉴스 서비스의 `/review`에서 연다. n8n 관리 주소의 `/review`가 아니다. 운영자 인증을 사용하고 키를 URL·localStorage에 저장하지 않는다. 필요할 때 승인·보류·제외하거나 대표기사·사건 묶음을 정정한다. 일간 정상 흐름은 이 화면의 수동 승인을 기다리지 않는다. 내용이 바뀌면 새 내용 해시로 다시 승인한다. 공개 Pages에는 편집 token이나 내부 API 주소를 넣지 않는다.

공개 발간 후 내용 변경은 revision을 올린 정정 호로 처리한다. 일간 발간 항목과 자체 요약·정정 이력은 계속 보관하며 원천 텍스트에는 별도 보존 정책을 적용한다.

## 4. n8n import와 연결

`automation/n8n`의 7개 JSON을 기존 인스턴스에 비활성 상태로 가져온다. 뉴스 서비스 기준 URL을 바꾸려면 export 생성기를 사용할 수 있다.

```powershell
python scripts/wind_news/build_workflows.py --service-url http://news-service:8090
```

1. n8n에 `BRE Wind Service`라는 Header Auth credential을 만든다. 헤더 이름은 `Authorization`, 값은 `Bearer <뉴스 서비스 토큰>`이다. 실제 값은 credential 화면에만 입력한다.
2. 모든 HTTP Request 노드에 이 credential을 연결하고 각 `Configure` 노드의 `service_url`을 검증한다. export에는 인스턴스별 credential ID가 없다.
3. `BRE-WIND-05A-Errors`를 각 업무 workflow의 Error workflow에 지정한다.
4. fixture로 수동 실행하여 job이 `QUEUED → RUNNING → SUCCEEDED`로 진행하는지 확인한다. 실패 시 `FAILED`와 정제된 오류 코드가 표시된다. polling에는 시간 제한이 있다.
5. 검토·발간 절차와 지정 테스트 채널을 검증한 후 필요한 스케줄만 게시한다. Error Trigger는 수동 실행 실패만으로 동작을 검증할 수 없으므로 발송 없는 자동 시험 workflow로 확인한다.

| workflow | 기본 일정(KST) | 역할 |
|---|---|---|
| 01 Collect | 매시 05분, 07:30 | 수집 작업 접수·완료 조회 |
| 02 Prepare | 07:40 | DB의 수집 결과를 고정하고 초안 준비 |
| 03 Publish | 08:00 | 승인 확인·웹 보고서와 검색 게시·실제 반영 확인 |
| 04 Deliver | 08~23시 5분마다 | 당일 웹 완료 호의 알림만 처리 |
| 05A Errors | 연결된 자동 실행 실패 | 복구 작업 호출 |
| 05B Reconcile | 5분마다 | 누락·중단 작업 점검. 자동 복구는 정책으로 제어 |
| 06 Backup | 02:30 | 뉴스 DB의 일관된 백업 |

설계안에서 n8n이 직접 Teams webhook을 호출하던 부분은 **n8n → 인증된 뉴스 서비스 → Teams**로 구현했다. webhook을 export나 실행 데이터에 넣지 않고, 송신 전 claim과 결과 저장을 한 서비스에서 관리하기 위한 선택이다. 웹 게시 workflow는 Teams workflow를 기다리지 않는다.

## 5. GitHub Pages 게시

뉴스 전용 clone을 준비한다. 기존 공지 publish clone·개발 checkout·release 경로를 지정하지 않는다. 전용 clone 루트에 빈 `.wind-news-publish-clone` 표식을 만들어 사용 의도를 명시한다. 이 파일은 commit하지 않는다. Git 사용자 정보와 저장소 쓰기 권한을 서비스 계정에 구성하되 명령행 URL에 token을 넣지 않는다. `main` merge와 서버 연계가 완료되기 전에는 기능 브랜치 push를 운영 발간으로 해석하지 않는다.

publisher의 `enabled`, `publish_clone` 또는 대응 환경변수, `remote`, `branch`, `pages_base_url`을 설정한다. 기본 대상 branch는 `main`이지만 최초 연계는 별도 staging 저장소/branch와 해당 Pages 주소로 검증한다. 실제 Pages source와 빌드 인증 조건을 확인한 뒤 운영 대상으로 전환한다.

publisher는 `data/wind-news/**`만 commit한다. 호 파일·검색 파일은 immutable이며 동일 호/동일 revision의 내용 변경을 거부한다. 새 호를 저장해도 과거 호를 지우지 않고, 과거 날짜를 복구해도 최신 호 포인터를 과거로 돌리지 않는다. 수정된 월의 검색 파일과 manifest를 함께 게시한다.

실제 Pages의 호·검색 manifest·파일 해시가 맞아야 `WEB_VERIFIED`다. Git push까지만 성공한 `COMMITTED`는 Teams 정상 발간 대상이 아니다. Pages가 늦게 반영되면 확인 단계를 재개한다.

CI는 기능 브랜치와 PR에서도 검증하고 `main`에서만 기존 서버 배포 조건을 평가한다. 뉴스 데이터만 변경된 commit은 Windows 코드 재배포를 일으키지 않는다. 기존 공지 JSON·키워드 계약은 유지한다.

## 6. Teams 결과와 재전송

Teams의 2xx는 `ACCEPTED`(접수)로 기록한다. 채널 메시지 ID 등 증거가 있으면 `DELIVERED`로 확정한다. 전송 timeout·연결 단절·불명확한 서버 응답은 `UNKNOWN`으로 보관하고 무조건 재전송하지 않는다. 전송 성공 직후 프로세스가 중단된 claim도 복구 시 불명확 상태로 처리한다. 명확한 거절인 `FAILED`만 최대 3회까지 시도한다.

뉴스용 Teams Workflows가 준비되지 않으면 전달만 `SKIPPED`가 되고 DB·웹 발간은 유지한다. 지원 webhook 호스트는 Microsoft의 `*.logic.azure.com`, `*.api.powerplatform.com`이다. 다른 테넌트 인증 방식은 별도 adapter 검증이 필요하다. 업무 채널과 테스트 채널을 구분하고 소유자·공동 소유자를 지정한다.

## 7. 백업·복구와 무인 운영

뉴스 백업 job은 단일 writer에서 checkpoint한 일관된 DuckDB 사본과 해시 정보를 만든다. 로컬 백업은 장애 대비의 첫 단계다. 생성된 백업과 비밀값 없는 정책·workflow export를 다른 물리 저장소에 암호화 복사하고, 기존 n8n DB·암호화키 백업은 실제 n8n 설치 방식에 맞춰 별도로 유지한다. 뉴스의 WF-06만으로 n8n 자체 DB와 credential 복구가 완성되는 것은 아니다.

복원은 실행 중 DB를 덮어쓰지 않고 새 파일로 검증한다.

```text
python scripts/wind_news/restore_backup.py <백업.duckdb> --sha256 <백업 해시> --output <새 격리 DB 경로>
```

격리 환경에서 복원 DB의 호를 읽어 `news.publisher.build_snapshot`으로 공개 자료를 재생성하고 건수·참조·해시를 대조한다. 검증 뒤 운영 서비스의 정지·경로 전환은 별도 유지보수 작업으로 수행한다. 복구 목표는 RPO 24시간·RTO 4시간이며 아직 서버 실측 결과가 아니다.

Docker 재시작 정책만으로 Windows 로그아웃·재부팅 후 무인 구동이 보장되지는 않는다. 서버에서 로그인 없이 10분 내 복구·로그아웃 후 24시간 지속을 확인해야 한다. 이번 코드는 기존 예약 작업·runner·전원 설정을 자동 변경하지 않는다.

## 8. 개발 검증

기존 scraper 테스트를 포함한 통합 검증 환경에는 양쪽 requirements를 설치한다. 서버 scraper 환경에는 뉴스 DB 패키지를 강제로 추가하지 않는다. 해당 패키지가 없는 환경에서는 DB 전용 테스트가 명시적으로 skip된다.

```powershell
.\.venv-news\Scripts\python.exe -m pip install -r requirements.txt -r news\requirements.txt
.\.venv-news\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_wind_news_frontend.js
python -m compileall -q news scripts/wind_news
```

단위 테스트는 임시 DB·모의 응답·격리 Git 원격을 사용하며 뉴스 실수집·유료 API·운영 Git push·Teams 실발송을 하지 않는다. 실제 API 품질, 7일 비공개 검토, n8n import 실행, 서버 무인 복구, Pages/Teams 첫 발간은 운영 연계 단계의 별도 검증이다.

개발 PC에서 데스크톱·360px 모바일 브리핑과 누적 검색을 실제 렌더링해 확인했다. 27,375건의 5년치 합성 이력은 공개 데이터와 분리하여 검색·기간·복합 필터·정정 처리를 검증했다. Docker 이미지 build와 실제 n8n import는 이후 서버에서 수행했다(9절). OAuth 어댑터는 모의 CLI로 인증 구분·두 추론 단계·근거 거절·캐시·지속 한도를 검증했으며 실제 계정 호출은 최초 로그인 후 확인한다.

2026-09-30 최종 로컬 검증: Python 전체 192개, Node 웹 모듈 16개 통과. 백업 복원 후 공개 호·월별 검색 파일의 해시가 원본과 동일함을 확인했다.

## 9. 실제 서버 설치 이력 — 2026-09-30

사용자의 후속 설치 요청에 따라 기존 `desktop-evu6usl-bre` GitHub 실행기로 사전 점검 후 설치했다. SSH·WinRM은 추가로 열지 않았다.

| 항목 | 확인한 상태 |
|---|---|
| Windows / WSL | `DESKTOP-EVU6USL` / `Ubuntu-24.04`, Linux 사용자 `n8nops` |
| 기존 n8n | Docker 29.8.1, n8n 2.40.7, 기존 PostgreSQL 유지 |
| 뉴스 컨테이너 | `bre-wind-news-news-service-1`, 비특권 UID 10001, `unless-stopped` |
| 전용 경로 | `/home/n8nops/bre-wind-news/{releases,runtime,publish,codex-auth,settings}` |
| 내부 통신 | 기존 `n8n-personal_outbound` network, `http://news-service:8090` |
| 설치 버전 | Codex CLI 0.159.2 / DuckDB 1.5.6 / Polars 1.44.2 |
| 상태·백업 | 인증 없는 호출 401, 인증된 ready 정상, 실제 DB 백업 해시·읽기 검증 통과 |
| 초기 데이터 | 실기사 0건·발간 호 0건. 인증 전 가상 기사는 게시하지 않음 |
| n8n | 전용 Header Auth를 연결하여 7개 workflow import, 서버 DB에 암호화 저장. 실제 백업 전체 노드 성공, 06-Backup은 매일 02:30 KST 게시 완료 |

실제 n8n 실행에서 발견한 Configure 노드의 JavaScript 괄호 오류를 수정하고 생성된 모든 Code 노드를 Node.js로 구문 검사하는 회귀 검증을 추가했다.

### 최초 인증 입력

다음은 **서버 PC의 사용자 PowerShell**에서 실행한다. 인증 코드·키를 채팅이나 GitHub Actions 입력값으로 보내지 않는다.

OpenAI 계정 로그인:

먼저 ChatGPT `설정 → Security and login`에서 `Enable device code sign-in for Codex, Excel, PowerPoint, and Word`를 켠다. 꺼져 있으면 OpenAI 동의 화면의 Continue가 비활성화된다. 설정을 켠 뒤에는 기존 화면만 새로고침하지 말고 서버에서 새 기기 인증 요청을 발급한다. 계정 보안 설정 및 최종 계정 승인은 소유자가 확인한다.

```powershell
wsl -d Ubuntu-24.04 --exec docker exec -it -e CODEX_HOME=/var/lib/bre-wind/codex-auth bre-wind-news-news-service-1 codex login --device-auth
```

서버에 직접 로그인할 수 없으면 `news_action=oauth`를 실행한다. 공식 Codex CLI가 생성한 단기 기기 코드를 기존 Tailscale Taildrop으로 같은 계정의 `nb01-PF4JSBDE`에 전송한다. Actions에는 코드·토큰·인증 파일을 출력하지 않는다. 수신 파일 이름은 `bre-wind-device-<session>.json`이며 파일에 있는 공식 OpenAI 주소에서 계정 소유자가 인증한다. 요청은 최대 12분 후 종료되고 서버 임시 로그는 제거된다. 인증 완료 후 `verify`로 실제 두 단계 요약을 검증한다. `auth.json`을 읽거나 복사하지 않는다.

OAuth 연결 여부와 모델 호출 성공은 별도로 검증한다. API에 보내는 생성 스키마는 호환 가능한 구조만 포함하며, 문자열 길이·배열 중복 등의 추가 제약은 결과 수신 후 전체 JSON Schema로 검사한다. 제약을 위반한 결과는 독립 근거 검증이나 발간 단계로 넘기지 않는다. 서버 진단은 CLI 원문 오류 대신 고정 분류 코드만 출력한다. [Structured Outputs 제약](https://developers.openai.com/api/docs/guides/structured-outputs#some-type-specific-keywords-are-not-yet-supported).

네이버 앱 등록·키 입력은 중단했다. `setup_naver.py`는 기존 경로로 실행해도 키를 요청하지 않는 중단 안내로 교체했다. 약관 동의나 키 발급으로 현재 사용 목적의 제한이 해소되지 않는다. 수집·게시 일정은 대체 수집원과 최초 발간 검증 전까지 비활성이다.

게시용 SSH 키는 뉴스 컨테이너 안에서 새로 생성했다. 사용자의 명시적 승인 후 `BRE Wind News server publisher` deploy key(등록 ID `164912220`)를 해당 저장소에 read-write로 등록했다. GitHub deploy key는 파일 경로별 권한을 제공하지 않으며, `data/wind-news/**` 제한은 publisher 코드가 적용한다. 키의 비밀 부분은 서버 runtime 밖으로 꺼내지 않는다.

PR #7을 main에 병합한 commit은 `6a83e3a089c92e764ca7584ec6f09043047c5ffe`다. GitHub Pages 배포가 성공했고 운영 주소의 오늘 브리핑·누적 검색을 브라우저에서 확인했다. 최초 발간 전에는 발간 준비 상태를 표시한다. 이후 서버 직접 접속 없이 사용자 PC에서 공식 기기 인증을 완료하도록 Taildrop 전달 방식을 추가했다. 수집·요약·발간·알림 일정은 인증 및 수집원 이용조건 확인 전까지 비활성으로 유지한다.

2026-09-30 배포 이력: `ef2a4f70bd882c599b145779dfe546f9a6f21952`의 코드가 main·작업 브랜치에 반영되고 기존 서버·GitHub Pages·뉴스 전용 컨테이너 배포가 모두 성공했다. 당시 로컬 Python 197개 및 웹 모듈 16개 테스트와 GitHub CI가 통과했다. 이후 사용자가 OpenAI 계정 승인을 완료했다.

2026-09-30 최신 검증: 출력 스키마 호환성을 수정한 `3a83e5d`를 [설치 run 36723567737](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36723567737)로 뉴스 서버에 배포했다. 로컬 Python 198개 테스트 및 GitHub CI가 통과했다. [검증 run 36724127866](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36724127866)에서 `CODEX_OAUTH_CONNECTED=true`, 실제 CLI 실행 2회 모두 종료 코드 0, `CODEX_TWO_PASS_RESULT=CODEX_VERIFIED`를 확인했다. 요약은 `gpt-6.1-sol/low`, 독립 근거 검증은 `gpt-6.1-sol/medium`이다. 비공개 합성 입력으로 연결을 검증했으며 뉴스 기사로 게시하지 않았다. 계정 재인증은 현재 필요하지 않다.

당시 검증에서 서비스 `ready/healthy`, 실제 DB 백업 해시, `FREE_ONLY_POLICY=true`, `PUBLISH_REPO_ACCESS=true`, n8n 비인증 접근 `401`이 정상이었다. DB는 실기사 0건·발간 호 0건, `COLLECTION_ENABLED=false`였다. 이 대기 상태는 아래 공개 기사 운영 개시로 대체됐다.

2026-09-30 공개 기사 운영 개시: 후속 사용자 지시에 따라 계약·유료 API 문의를 선행 조건에서 제외했다. 연합뉴스 RSS, 전기신문·에너지신문의 공개 목록과 원문을 사용하며, 외부 유료/무료 식별값 없이 일반 공개 본문의 접근을 확인한다. [첫 발간 run 36730327242](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36730327242)에서 기사 11건 DB 적재, 마감 대상 7건 OAuth 요약·독립 검증 통과, 보류 0건, `wind-2026-09-30` 첫 호 Git 게시를 완료했다. 공개 파일의 SHA-256과 브라우저의 7개 기사·원문 링크도 확인했다. 목록 조회 한도와 페이지 반복 때문에 수집 범위는 `partial`로 표시한다. 근거 검증 실패를 뜻하지는 않는다.

2026-10-01 정정 검증: 본문의 부수적인 안전 표현이 기사 주제보다 우선하던 분류를 제목·도입부 우선으로 수정했다. [정정 run 36785268919](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36785268919)에서 2026-09-30 revision 2가 `WEB_VERIFIED`로 완료됐다. 기존 7건과 요약·원문 링크는 유지하고 정정 이유를 공개했다. 로컬 전체 Python 209개와 웹 모듈 16개 테스트가 통과했다. 뉴스 전용 라이브러리가 없는 기존 공고 환경에서는 해당 DB 테스트만 기존 방식대로 분리한다.

추가로 `약해지는` 등의 일반 표현을 계약 해지로 판정하지 않도록 계약 문맥을 요구하는 회귀 검증을 추가했다. [정정 run 36788367752](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36788367752)에서 미게시 초안의 분류 2건을 감사 이력이 남는 API로 수정하고, 2026-09-30 revision 3의 `WEB_VERIFIED`를 확인했다. 이미 OAuth 검증을 통과한 항목만 대상으로 했으며 보류 항목을 승인하지 않았다. 최신 전체 Python 210개 및 웹 모듈 16개 테스트가 통과했고 [main CI·서버 배포 run 36788497091](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36788497091)이 성공했다.

2026-10-01 정기 게시 확인: `wind-2026-10-01` revision 1이 08:00:02 KST에 자동 게시됐다. 마감 대상 4건 중 3건 발간·1건 자동 검증 보류이며 원문 링크·공개 JSON 해시를 검증했다. 9월 30일 7건과 합쳐 웹 발간 기사는 10건이다. 수집 범위 제한 및 보류 항목 때문에 보고서는 `partial`로 표시한다. 브라우저 검색에서 정정 전후 자료가 중복되지 않는 것도 확인했다.

| 활성 n8n 일정 | Asia/Seoul 기준 |
|---|---|
| Collect | 매시 05분 및 07:30 마감 수집 |
| Prepare | 매일 07:40 OAuth 요약·검증 |
| Publish | 매일 08:00 웹 게시 |
| Reconcile | 5분 간격 실패·게시 검증 재시도 (`auto_recover=true`) |
| Backup | 매일 02:30 |

위 5개 workflow의 `Published` 상태를 n8n 화면에서 확인했다. Deliver는 Teams 전용 연결 전까지 비활성이며, Errors는 다른 workflow에서 참조하는 오류 처리용이다. 계정 소유자의 추가 로그인이나 일일 수동 승인은 필요하지 않다.

마지막 [서버 검증 run 36789062167](https://github.com/jung372/BRE-Workflow-Automation2/actions/runs/36789062167)은 성공했다. DB 백업 해시·읽기 검증에서 기사 11건, 발간 revision 레코드 4건(9월 30일 r1–r3와 10월 1일 r1)을 확인했다. 서비스 `ready/healthy`, OAuth 연결·2단계 검증, 무료 공개 기사 정책, 수집 활성, Git 게시 권한이 모두 정상이며 n8n의 무인증 접근은 401로 차단된다.

### 반복 가능한 서버 작업

기존 `ci-deploy.yml`을 수동 실행할 때 `news_action`을 지정한다.

| 값 | 동작 |
|---|---|
| `preflight` | 비밀값 없는 Windows·WSL·Docker 사전 점검 |
| `install` | 전체 테스트 후 뉴스 서비스만 설치·갱신 |
| `connect` | 전체 테스트 후 뉴스 전용 게시 clone·credential·workflow 연결. 지정 뉴스 workflow는 비활성 draft로 갱신 |
| `verify` | 인증·백업·n8n 내부 통신·연결 상태 검사. OAuth 연결 시 비공개 입력으로 2단계 요약 검사 |
| `oauth` | 서버에서 공식 기기 로그인 시작, 같은 소유자의 PC에 단기 인증 안내를 Taildrop 전송 |
| `launch` | 공개 매체 수집과 Git 게시를 활성화하고 실제 수집 → OAuth 요약·검증 → 첫 호 게시. 기존 뉴스 서비스만 재시작하며 n8n 스케줄 활성화는 별도 확인 |
| `correct` | `launch`와 같은 공개 수집 경로에서 가장 최근 발간 호의 분류를 재계산하고 정정 사유가 표시되는 새 revision 발간 |
| `normal` | 기존 CI와 main의 공고 서버 배포 |

현재 별도 검증이 남은 항목은 Teams 수신과 Windows 재부팅·로그아웃 후 지속 운영이다. Teams 전용 연결은 미설정이며 웹 발간·검색은 독립적으로 운영한다.
