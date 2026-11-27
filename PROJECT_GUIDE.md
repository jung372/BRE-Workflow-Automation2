# BRE 개발 가이드

## 근거와 구조
[README](README.md), [배포 설계](docs/PRD-auto-deploy.md), [CI](.github/workflows/ci-deploy.yml), [의존성](requirements.txt).
tests는 config·detector·state·runtime·push의 오프라인 회귀다. run_local.py는 락·실행·발행, notify_teams.py는 실제 전송 진입점이다.

## 환경과 검증
프로젝트 루트의 개발 Python 환경에서:

~~~powershell
python -m unittest discover -s tests -p "test_*.py" -v
~~~

최초 환경은 requirements.txt를 따른다. Playwright Chromium은 브라우저가 필요한 승인된 작업에만 설치한다.
파서는 저장 HTML, 판정은 이전/현재 fixture, 전송은 mock으로 확인한다. 진입점 import도 부작용을 확인한 뒤 수행한다.

## 회귀와 완료 기준
공고 ID·시각·URL·중복 판정·상태 JSON 호환성과 KST 날짜 경계를 보존한다.
timeout·부분 실패·빈 응답을 삭제·신규로 오인하지 않게 한다.
배포기는 실수집 smoke와 포인터 전환을 포함하므로 단위 테스트 대용으로 실행하지 않는다.
문서만 변경할 때의 paths-ignore는 현재 CI를 확인한다. 오프라인 통과와 실수집·발행·Teams 수신 검증을 구분한다.
