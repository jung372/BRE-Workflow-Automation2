# Teams 정기 보고 운영

GitHub `schedule` 지연을 피하기 위해 Windows 서버의 기존 수집 계정에서
`BRE Teams Morning Report` 작업을 실행한다. 뉴스 브리핑의 서버 시계 기반
재시도·마감·하루 중복 방지 원칙을 사용한다. 뉴스의 n8n 일정은 변경하지 않는다.

- 평일 KST 08:05~09:00에 5분 간격으로 실행한다.
- 08:05~08:25에는 준비 상태만 확인한다. 데이터 미확보 시 08:40까지 실행 중이
  아닌 기존 `BRE Scraper` 작업을 시작한다. 수집기의 잠금·publish 경로를 재사용한다.
- 발송은 08:30 시작, 실패 시 08:35/40/45/50/55에 재시도한다. 실제 전송 직전에도
  시각을 검사하며 08:59:30부터 새 요청을 차단한다. 09:00에는 마감 상태만 기록한다.
- 금일 08시와 전 평일 08시 스냅샷을 비교한다. 신규 글이 없고 계측기도 정상이면
  `NO_NEW_ITEMS`로 생략한다. 기존 계측기 장애 알림은 유지한다.
- 수집 실패·스냅샷 부재·손상은 `DATA_NOT_READY`로 구분한다. 일부 사이트만
  실패해도 비교 가능한 사이트의 신규 글은 발송하고 카드에 미확보 사이트를 표시한다.
  기준 스냅샷이 없는 사이트를 전체 신규 글로 처리하지 않는다.
- `ACCEPTED`와 `UNKNOWN`은 자동/수동 모두 재발송하지 않는다. 명확한 HTTP 거부와
  연결 시간 초과만 다음 슬롯에서 재시도한다. 응답 대기 중 단절, 5xx 또는 전송 중
  프로세스 종료는 `UNKNOWN`이며 운영 확인이 필요하다. HTTP 접수는 실제 Teams 표시
  확인과 다르다. 불명확한 결과를 무조건 재전송하면 중복 알림이 발생할 수 있다.

## 배포 및 검증

1. main의 `검증 및 서버 배포`가 성공해 새 릴리스가 활성화된 것을 확인한다.
2. `Teams 정기 보고 서버 예약 설치·검증` (`install-report.yml`)을 `action=install`로
   실행한다. GitHub Secret은 출력 없이 DPAPI로 암호화해 runtime의
   `teams-report/webhook.clixml`에 저장한다. 수집 계정과 SYSTEM만 접근 가능하다.
3. `REPORT_SCHEDULE_VERIFIED`, 다음 실행 시각, `PREVIEWED` 데이터를 확인한다.
   설치 검증은 실제 Teams 요청을 보내지 않는다. `action=verify`는 재설치 없이 확인한다.
4. `report.yml`은 `preview=true` 기본값으로 실제 publish 데이터·오늘 발송 상태를
   확인한다. `preview=false`는 명시적인 수동 발송이며 시간 제한만 해제한다.
   `force_send=true`도 하루 중복 방지는 해제하지 않는다.

발송 기록은 runtime `teams-report/YYYY-MM-DD.json`, 로그는
`logs/teams-report-YYYY-MM-DD.log`에 보관한다. 배포·롤백으로 초기화하지 않는다.
락은 프로세스 종료 시 OS가 해제하며, POST 전 `UNKNOWN` 기록을 먼저 내구 저장한다.
launcher 수정 시 설치를 다시 실행해 설치 루트의 launcher 사본도 갱신한다.

예약 작업은 기존 수집 계정의 Interactive 로그온 조건을 유지하고 WakeToRun을 사용한다.
서버 전원·로그온·네트워크와 Teams 수신 서비스가 동작해야 한다. 외부 장애까지 포함한
절대 수신 시각을 보장하지 않으며, 09:00 이후 자동 늦은 발송으로 숨기지 않는다.

## 원인 확인 (2026-10-19)

기존 GitHub 08:30 예약은 10월 14일 11:02:38, 15일 10:37:36,
16일 10:33:06 KST에 시작했다. 16일 실행 `38835729737`은 신규 2건을
10:33:17에 처리했다. 발송 처리보다 예약 이벤트 시작 지연이 주원인이었다.
GitHub의 [예약 실행 지연 안내](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)를
참고한다. 기존 GitHub cron을 제거해 서버 예약과 중복 발송하지 않도록 한다.
