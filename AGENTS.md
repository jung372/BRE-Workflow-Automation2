# BRE Workflow Automation 작업 지침

## 범위
공고·계측기 수집, GitHub Pages 데이터 발행과 Teams 알림을 연결하는 자동화다.

사용자 프로필의 공통 AGENTS.md를 기본으로 적용한다. 작업 전에 [PROJECT_GUIDE.md](PROJECT_GUIDE.md)를 읽어 명령·근거·운영 경계를 확인한다.

## 프로젝트 계약
- scrapers/ 수집, logic/ 판정·실행, state.py 상태, presentation/ 카드 생성의 책임을 유지한다.
- releases 코드, BRE_Workflow_runtime/publish 발행 clone, runtime 설정·로그·락을 분리한다.
- last_state.json·data/status.json은 Git 추적 중인 실제 상태다. 테스트로 덮어쓰지 않는다.
- run_local.py --no-push도 수집·로컬 쓰기를 수행한다. 읽기 전용 점검으로 취급하지 않는다.
- Teams 전송·원격 push는 명시된 요청 범위에서만 수행한다. 테스트는 오프라인 fixture·mock·임시 상태를 사용한다.
- 수집 실패와 신규 공고·계측기 Offline을 구분하고 중복 알림 방지·Asia/Seoul 시간대를 유지한다.

## 검증과 완료
- 가이드의 변경별 검증을 실행하고 실제 명령·결과를 보고한다. 미실행을 통과로 기록하지 않는다.
- 문서 변경은 경로·링크·현재 manifest와 CI의 일치를 확인한다.
- 운영 변경은 요청의 승인 범위를 확인하고, 현재 파일을 기준으로 오래된 지침을 함께 정정한다.
