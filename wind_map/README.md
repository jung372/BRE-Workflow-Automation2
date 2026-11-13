# 풍력대동여지도

기존 BRE 사이트의 #/wind-map 및 #/wind-map/project/{project_id}에 연결되는 육상풍력 허가·근거 조회 모듈이다. 운영 서버의 비공개 SQLite와 원본에서 정적 JSON을 생성한다. 기존 공지·뉴스·DART 상태는 공유하지 않는다.

## 설치와 실행

Python 3.11 이상. 프로젝트 루트에서 별도 가상환경에 설치한다.

~~~powershell
python -m venv .venv-wind-map
./.venv-wind-map/Scripts/python.exe -m pip install -r requirements.txt -r wind_map/requirements.txt
./.venv-wind-map/Scripts/python.exe -m wind_map --help
~~~

이후 python 명령은 활성화한 전용 가상환경 기준이다. 아래 $runtime은 실제로 선택한 **비공개 로컬 디렉터리**다. 공개 저장소·동기화 폴더를 지정하지 않는다. --runtime 생략 시 BRE_WIND_MAP_RUNTIME, 이어서 BRE_RUNTIME_DIR/wind-map을 사용한다. 아무 설정도 없으면 중단한다.

~~~powershell
$runtime = 'D:/05 AI Study/BRE_Wind_Map_runtime'
python -m wind_map --runtime $runtime init
python -m wind_map --runtime $runtime status
~~~

init/status도 해당 DB를 연다. 단순 환경 점검에 임의의 운영 경로를 넣지 않는다.

## 구성

| 위치 | 책임 |
| --- | --- |
| store.py, schema.sql | 불변 원본, 페이지·행, 허가·사업, 검토 로그, 트랜잭션 |
| extract.py | 구·신 PDF 표, HWPX 텍스트, 선택적 OCR, 분기 후보 비교 |
| events.py, milestones.py | 효력일·확인일, 정정·취소·승계, 날짜 정밀도, 검사 범위 |
| aggregates.py, geography.py | 정수 W 합계, 허가 중복 제거, 미배분, 좌표 변환·경계 검사 |
| queue.py, sources.py, cycle.py | 영속 검색 창, 전체 페이지 대사, 재개·재조회, 상세·첨부 후보 |
| snapshot.py, publication.py | 공개 허용 필드, 발행 차단, 불변 버전, Git 충돌 재시도·검증 |
| backup.py | 일관된 DB와 참조 원본 백업, 새 런타임으로 복원 |
| ../assets/wind-map.js, wind-map.css | 지도·지역표·검색·상세·이전 정상본 복구 |

## 원본과 검토

1. 원본 파일과 메타데이터 JSON을 준비한다. 필수 키는 source_key, agency, title, url, checked_at(시간대 포함), visibility(public/restricted), format이다. 대장은 purpose=permit_register와 quarter=2026-Q3처럼 표시한다. PDF 쪽수는 등록 명령이 실제 파일에서 읽는다.
2. 출력된 document_id로 전 페이지를 추출한다. 같은 게시 ID·파일 해시는 다시 등록해도 같은 문서다. 교체 첨부는 새 해시와 previous_version으로 연결한다.
3. 후보·대상외·머리글·빈행·검토대기 행을 원문과 대조한다. 이월 좌표·반복 번호·동일 기호·풍력 세부 유형을 추측해서 연결하지 않는다.
4. 사업·허가·근거·사건·지역배분을 검토 JSON으로 입력한다. 자동 추출이나 검색 제목만으로 사건을 승인하지 않는다.
5. 페이지 검토, 정답 표본, 원행-허가 대사를 모두 통과해야 운영본을 만들 수 있다.

~~~powershell
python -m wind_map --runtime $runtime register 'source.pdf' --metadata 'source-metadata.json'
python -m wind_map --runtime $runtime extract DOCUMENT_ID
python -m wind_map --runtime $runtime export-review "$runtime/review.json"
python -m wind_map --runtime $runtime import 'reviewed-bundle.json' --actor '검토자' --reason '원문 대조'
~~~

입력 bundle은 schema_version=1과 projects, regions, evidence, permits, events, allocation_sets, milestones, coordinates, coverage, validations 중 필요한 배열로 구성한다. 구조 예시는 [합성 fixture](../tests/wind_map_helpers.py)에 있다. **시험 값을 실제 운영 자료로 가져오지 않는다.**

- 허가 number는 관할에서 유일한 연도 포함 원문 번호다. 옛 반복 번호는 연도·사업자·소재지 대조 전 합치지 않는다.
- capacity_w는 정수 W다. 60 MW는 60000000. 새 용량과 종전 용량을 보존한다.
- 사건·배분·마일스톤·좌표는 known_at, date, review_state, evidence_id가 필요하다. 날짜는 start/end/precision/estimated를 모두 가진다. 월 단위는 그 달의 범위이며 임의 일자를 확정하지 않는다.
- 사실 정정은 새 ID와 supersedes를 쓴다. 원문·기존 사건을 덮어쓰지 않는다. 문서 메타데이터는 document-metadata 명령으로 감사 기록과 함께 정정한다.
- 근거는 공개 문서의 정확한 쪽·문서번호·절, 인용 구절, publication_approved=true를 갖춰야 한다. 제한 회신은 내부 검토에만 남긴다.
- construction_plan_deadline은 인가일이 아니다. 최초·변경 인가는 별도 construction_plan 마일스톤과 disposition_kind=initial/amendment, 필요 시 round로 입력한다.
- 환경평가는 assessment_type=main/small/strategic와 stage=draft/main/reconsultation/change, 적용 permit_ids를 명시한다.
- 검사는 round와 equipment ID 목록을 기록한다. 사업 equipment 배열에 설비별 정격 W·근거·지역이 있어야 검사 확인 용량을 산출한다. 검사 신청·준공은 별개다.
- 지역별 coverage는 complete/central_only/partial/not_collected다. 시군구 미확정 물량은 알려진 상위 지역의 미배분으로 보존한다.
- 좌표는 발전소 소재지 근거만 허용한다. x/y/crs, kind, region_check를 기록한다. region_geometry가 있으면 WGS84 GeoJSON 경계도 검사한다. 예외는 boundary_exception과 사유가 필요하다. 주소 지오코딩 결과도 검토 후 parcel로 입력한다.

문자층 없는 PDF는 설치된 Tesseract와 kor+eng 언어 데이터로 OCR할 수 있다. 도구를 자동 설치하거나 OCR 값을 자동 승인하지 않는다.

~~~powershell
python -m wind_map --runtime $runtime ocr-page DOCUMENT_ID 860 --language kor+eng
python -m wind_map --runtime $runtime review-page DOCUMENT_ID 860 'page-review.json' --actor '검토자' --reason '원본 이미지 대조'
~~~

페이지 검토 파일에는 source_sha256, image_reviewed=true, method, rows가 필요하다. 이미지로 공란을 확인했다면 blank=true를 넣는다. 모든 행의 분류를 명시한다. 승인된 면 정정은 reopen-page로 먼저 사유를 기록한다. 옛 행은 감사 로그에 남고 기존 발행 승인은 무효화된다.

HWPX는 텍스트를 후보로 적재한다. 바이너리 HWP는 사용 가능한 변환 도구로 PDF를 만든 뒤 원 HWP와 변환본을 별도 문서로 등록하고 parent_document_id로 연결한다. 이 변환은 현재 자동 실행하지 않는다.

validations는 단순 승인 표시만으로 통과하지 않는다. export-review의 현재 review_digest, 원본 source_sha256, 검토자·확인 시각이 일치해야 한다.

- row_reconciliation: 모든 후보 행을 정확히 한 번 연결한 row_links(row_id/permit_id/evidence_id).
- golden_samples: 비어 있지 않은 checks(table/record_id/field/expected). 실제 저장값과 기대값을 다시 비교한다. field는 date.start처럼 점으로 구분할 수 있다.

## 전국 검색과 정시 수집

~~~powershell
python -m wind_map --runtime $runtime cycle --sources config/wind-map-sources.example.json --live --max-jobs 5
python -m wind_map --runtime $runtime enqueue-projects --sources config/wind-map-portal.example.json --end 2026-11-12
python -m wind_map --runtime $runtime collect --sources config/wind-map-portal.example.json --portal --live --max-pages 10
~~~

종료일은 실제 실행일로 바꾼다. 위 명령은 외부 사이트 조회와 비공개 DB 쓰기를 수행한다. --live는 해당 실행을 명시하는 옵션이다.

cycle은 지정한 수만큼 작업하고 종료한다. 중앙 공지 검색에서 상세·첨부 작업을 만들며 다음 실행에서 우선 처리한다. 완료한 오늘 구간을 1시간 뒤 다시 조회할 때 이전 페이지 이력을 search_runs에 보관한다. 과거 연도 완료 구간을 매시간 재수집하지 않는다. 기존 관심사업 키워드·90일 모니터링 설정은 변경하지 않는다.

- KOREC 예시는 실제 게시판의 숨김 게시 ID, 고정글과 일반 10행, 첨부 필드를 사용한다. 날짜 검색이 없는 게시판은 전체 검색 결과를 대사한 뒤 창을 완료한다.
- 정보공개포털은 별도 Playwright 세션에서 페이지 쿠키를 얻고 rtnList/rtnTotal을 확인한다. 차단·로그인 화면을 0건으로 처리하지 않는다. Chromium 설치는 python -m playwright install chromium으로 별도 수행한다.
- EIASS·지자체는 kind, 공식 url/hosts, method, 검색·페이지·날짜 매개변수, total_selector, row_selector를 기관 응답에 맞춰 설정한다. EIASS는 세 평가유형별 source ID와 assessment_type을 분리한다. 선택자·평가 범위·TLS를 확인하기 전에는 전국 운영 수집을 켜지 않는다.
- 페이지 반복·건수 변경·불완전 종료는 blocked다. status에서 원인을 보고 retry JOB_ID로 재개하거나 split JOB_ID로 날짜 창을 나눈다. 하루 창도 넘치면 수동 조회가 필요하다.
- 확정값은 상세 원문 검토 후 입력한다. 수집 성공과 사건 확정은 별개다.

Windows 예약 작업용 진입점은 [collect.ps1](../scripts/wind_map/collect.ps1)이다. 전용 Python·비공개 Runtime·Sources를 인자로 전달한다. 이 구현 작업에서는 예약 작업을 설치하거나 기존 수집기를 재시작하지 않았다.

## 스냅샷·지도·발행

~~~powershell
python -m wind_map --runtime $runtime build --as-of 2026-11-12 --known-at 2026-11-12T09:00:00+09:00 --output 'D:/wind-map-staging'
python -m wind_map validate-public 'D:/wind-map-staging'
~~~

기준일과 자료 확인 시점을 구분한다. 예시 날짜를 실제 발행 기준으로 바꾼다. 검토 중 자료는 build --preview로만 만들 수 있고 화면에 검증용 표시가 붙는다. publish는 preview를 거부한다.

공개 경로는 data/wind-map/versions/{snapshot_id}/...와 마지막에 교체하는 current.json이다. manifest와 각 파일의 SHA-256을 확인한다. Windows 줄바꿈 변환을 막기 위해 .gitattributes에서 이 JSON을 -text로 지정했다.

카카오는 [공개 설정](../config/wind-map-public.json)의 **JavaScript 앱키**만 읽는다. REST API 키·관리자 키는 여기에 넣지 않는다. 실제 도메인 등록과 지도 사용 설정은 [카카오 공식 문서](https://apis.map.kakao.com/web/documentation/)를 따른다. 앱키가 비어 있거나 SDK가 실패해도 지역표·검색·상세가 작동한다. 클러스터는 마커 수이며 서버의 지역 MW 합계와 독립적이다.

발행용 clone은 개발 worktree·기존 뉴스 발행 clone과 분리한다. .git/bre-wind-map-publisher 파일에 정확한 origin URL을 기록한 **전용 일반 clone**만 허용한다. clean 상태와 지정 브랜치를 검사하며 data/wind-map/만 stage한다. 동시 push 실패 시 최대 3회 fetch/rebase 후 재검증한다. 충돌 시 중단하고 로컬 커밋을 보존한다. 강제 push는 하지 않는다.

~~~powershell
python -m wind_map --runtime $runtime publish --as-of 2026-11-12 --clone 'D:/wind-map-publish' --remote 'https://github.com/jung372/BRE-Workflow-Automation2.git' --confirm-push
python -m wind_map verify-public-url 'https://jung372.github.io/BRE-Workflow-Automation2/' --expected-snapshot SNAPSHOT_ID
~~~

publish는 실제 외부 push다. 운영 승인이 있을 때 실행한다. push 성공과 Pages 제공 완료는 별개이며 두 번째 명령이 실제 응답 바이트를 확인한다. 전파 지연·실패 때 브라우저는 이전 정상본과 기준일을 표시한다. 로컬 포인터 복구는 rollback-pointer PUBLIC_ROOT다. 원격 복구에는 검토한 포인터를 별도로 발행해야 한다.

공개 데이터 변경은 코드 배포 trigger에서 제외하고 [별도 데이터 CI](../.github/workflows/validate-wind-map.yml)에서 검증한다.

## 백업과 검증

~~~powershell
python -m wind_map --runtime $runtime backup-bundle 'D:/wind-map-backups/backup-20261112'
python -m wind_map restore-bundle 'D:/wind-map-backups/backup-20261112' 'D:/wind-map-restored'
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/test_wind_news_frontend.js tests/test_solar_news_frontend.js tests/test_wind_map_frontend.js
~~~

백업 bundle은 SQLite backup API로 만든 DB와 그 DB가 참조하는 원본 해시를 함께 확인한다. 복원은 새 디렉터리에만 허용한다. backup 명령은 DB만 복사하므로 원본 포함 복구에는 backup-bundle을 쓴다. OCR 파생 이미지는 원본에서 재생성할 수 있다.

시험은 임시 DB·합성 사업·로컬 bare Git·저장된 원본 표본으로 실행한다. 실사이트 대량 수집, 운영 발행, Teams 전송을 시험에 포함하지 않는다. 실제 확인 상태와 남은 운영 조건은 [검증 기록](../docs/VALIDATION-wind-map.md)에 기록한다.
