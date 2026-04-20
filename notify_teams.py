"""
MS Teams 정기 보고 스크립트 (평일 09:00 KST)
==============================================
동작 흐름:
  1. last_state.json 에서 hourly_snapshots 읽기
  2. 전일 평일 08:00 스냅샷(A) vs 금일 08:00 스냅샷(B) 비교 → Set(B) - Set(A)
  3. data/status.json 에서 계측기 최신 상태 읽기
  4. 조건 충족 시 Teams MessageCard Webhook 발송
     - 조건 A: 신규 게시글 1건 이상
     - 조건 B: 오프라인 계측기 1대 이상
     - 두 조건 모두 미충족 시 발송 생략 (--force 플래그로 강제 발송 가능)

실행:
  TEAMS_WEBHOOK_URL=<url> python notify_teams.py           # 조건부 발송
  TEAMS_WEBHOOK_URL=<url> python notify_teams.py --force   # 조건 무시, 강제 발송

환경변수:
  TEAMS_WEBHOOK_URL  (필수) GitHub Secret에서 주입
  DASHBOARD_URL      (선택) 대시보드 바로가기 URL
  FORCE_SEND         (선택) "true" 설정 시 --force와 동일 (workflow_dispatch 입력용)
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

import requests

FORCE_SEND = "--force" in sys.argv or os.environ.get("FORCE_SEND", "").lower() == "true"

# ── 경로 및 상수 ──────────────────────────────────────────────────
KST           = timezone(timedelta(hours=9))
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATE_FILE    = os.path.join(BASE_DIR, "last_state.json")
STATUS_FILE   = os.path.join(BASE_DIR, "data", "status.json")
WEBHOOK_URL   = os.environ.get("TEAMS_WEBHOOK_URL", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://jung372.github.io/BRE-Workflow-Automation2/")

SITE_DISPLAY = {
    "notice":       "전기위 공지사항",
    "result":       "위원회 개최결과",
    "eiass_wind":   "소규모 환평(풍력)",
    "kepco_notice": "한전 재분배 용량 공지",
    "nie_notice":   "생태·자연도 공고",
}


# ── 전일 평일 계산 ────────────────────────────────────────────────
def prev_weekday(date):
    """월요일이면 3일 전(금요일), 그 외엔 1일 전을 반환."""
    return date - timedelta(days=3 if date.weekday() == 0 else 1)


# ── 윈도우 비교 ───────────────────────────────────────────────────
def get_new_items(state: dict):
    """
    금일 08:00 스냅샷(B) - 전일 08:00 스냅샷(A) 로 신규 아이템 추출.
    반환: (new_items_list, today_key, prev_key)
    """
    now       = datetime.now(KST)
    today     = now.date()
    prev      = prev_weekday(today)

    today_key = today.strftime("%Y-%m-%d") + " 08"
    prev_key  = prev.strftime("%Y-%m-%d") + " 08"

    all_new = []
    for site_id, site_state in state.items():
        if not isinstance(site_state, dict):
            continue
        hourly = site_state.get("hourly_snapshots", {})

        b_items = hourly.get(today_key, [])
        a_ids   = {item["id"] for item in hourly.get(prev_key, [])}

        for item in b_items:
            if item["id"] not in a_ids:
                all_new.append({
                    "site_name": SITE_DISPLAY.get(site_id, site_id),
                    "title":     item.get("title", ""),
                    "date":      item.get("date", ""),
                    "url":       item.get("url", "#"),
                })

    return all_new, today_key, prev_key


# ── 계측기 상태 로드 ──────────────────────────────────────────────
def load_metmasts() -> list:
    if not os.path.exists(STATUS_FILE):
        return []
    with open(STATUS_FILE, encoding="utf-8") as f:
        return json.load(f).get("metmasts", [])


# ── MessageCard 빌드 ──────────────────────────────────────────────
def build_card(new_items: list, metmasts: list) -> dict:
    offline = [m for m in metmasts if m.get("status") != "Online"]

    theme_color  = "E74C3C" if offline else "8FC31F"
    metmast_text = (
        f"⚠️ {', '.join(m['name'] for m in offline)} 연결 불가"
        if offline else "✅ 정상 작동"
    )

    sections = [
        {
            "facts": [
                {"name": "📡 계측기 작동상황:", "value": metmast_text},
                {"name": "📝 신규 게시글:",     "value": f"총 {len(new_items)}건"},
            ]
        }
    ]

    if new_items:
        post_facts = [
            {
                "name":  f"[{item['site_name']}]",
                "value": f"[{item['title']} ({item['date']})]({item['url']})",
            }
            for item in new_items
        ]
        sections.append({"title": "신규 게시글 목록", "facts": post_facts})

    return {
        "@type":      "MessageCard",
        "@context":   "https://schema.org/extensions",
        "themeColor": theme_color,
        "summary":    f"[정기 알림] 전일 대비 신규 게시글 {len(new_items)}건",
        "title":      "🚨 [정기 알림] 전일 대비 신규 게시글 현황",
        "sections":   sections,
        "potentialAction": [
            {
                "@type": "OpenUri",
                "name":  "📊 모니터링 대시보드 바로가기",
                "targets": [{"os": "default", "uri": DASHBOARD_URL}],
            }
        ],
    }


# ── 메인 ─────────────────────────────────────────────────────────
def main():
    if not WEBHOOK_URL:
        print("❌ TEAMS_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("❌ last_state.json 없음. scraper.py 를 먼저 실행하세요.")
        sys.exit(1)

    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)

    metmasts             = load_metmasts()
    new_items, b_key, a_key = get_new_items(state)
    offline              = [m for m in metmasts if m.get("status") != "Online"]

    print(f"비교 윈도우: {a_key}  →  {b_key}")
    print(f"신규 게시글: {len(new_items)}건 / 오프라인 계측기: {len(offline)}대")

    # 발송 조건 평가
    if not new_items and not offline:
        if FORCE_SEND:
            print("⚡ --force 모드: 조건 미충족이나 강제 발송")
        else:
            print("✅ 신규 게시글 없음 + 모든 계측기 정상 → 알림 생략")
            return

    card = build_card(new_items, metmasts)
    resp = requests.post(WEBHOOK_URL, json=card, timeout=15)

    # Teams Webhook 정상 응답: HTTP 200, body "1"
    if resp.status_code == 200 and resp.text.strip() == "1":
        print("✅ Teams 알림 발송 완료")
    else:
        print(f"❌ 발송 실패: HTTP {resp.status_code} / {resp.text[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
