"""
주요 사이트 공지 모니터링 스크래퍼 - GitHub Actions 전용
실행: python scraper.py
출력: data/status.json  (GitHub Pages 대시보드에서 읽음)

[새 게시글 판정 기준]
  실행 시점 기준 7일 전 게시글 목록과 현재 목록을 비교.
  현재 목록에는 있지만 7일 전 목록에는 없던 게시글 = 신규.
  - 7일치 데이터 미만이면 가장 오래된 저장 목록 기준
  - 첫 실행 시에는 오늘 목록만 저장하고 새 글 = 0
"""
import json, os, logging, requests, urllib3
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# SSL 경고 비활성화 (EIASS 사이트 특성 대응)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATE_FILE    = os.path.join(BASE_DIR, "last_state.json")
DATA_DIR      = os.path.join(BASE_DIR, "data")
OUTPUT        = os.path.join(DATA_DIR, "status.json")
KST           = timezone(timedelta(hours=9))
BASELINE_DAYS = 7   # 비교 기준: 며칠 전 목록과 비교할지
KEEP_DAYS     = 8   # 목록 보관 기간 (기준일 + 여유 1일)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SITES = [
    {
        "id": "notice", "name": "전기위 공지사항", "icon": "📢", "color": "#3b82f6",
        "url": "https://www.korec.go.kr/notice/selectNoticeList.do",
        "title_idx": 2, "date_idx": 3, "num_idx": 0,
    },
    {
        "id": "result", "name": "위원회 개최결과", "icon": "📋", "color": "#10b981",
        "url": "https://www.korec.go.kr/notice/result/selectNoticeList.do",
        "title_idx": 1, "date_idx": 2, "num_idx": 0,
    },
    {
        "id": "nie_notice", "name": "생태.자연도 공고", "icon": "🍃", "color": "#eab308",
        "url": "https://www.nie.re.kr/nie/bbs/BMSR00038/list.do?menuNo=200099&pageIndex=1&gubunCd=&searchCondition=&searchKeyword=",
        "title_idx": 1, "date_idx": 4, "num_idx": 0,
    },
    {
        "id": "kepco_notice", "name": "한전 재분배 용량 공지", "icon": "⚡", "color": "#f97316",
        "url": "https://online.kepco.co.kr/EWM040D00",
        "type": "kepco",
    },
    {
        "id": "eiass_wind", "name": "소규모환경영향평가(풍력)", "icon": "🌬️", "color": "#0ea5e9",
        "url": "https://www.eiass.go.kr/biz/base/info/perList.do?menu=biz&biz_gubn=M",
        "type": "eiass",
    },
]


# ── 게시글 고유 ID ──────────────────────────────────────────────
def item_id(n):
    return f"{n['num']}||{n['title']}"


# ── 상태 파일 ────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── 날짜별 게시글 목록 관리 ──────────────────────────────────────
def get_baseline_ids(site_state):
    """
    7일 전 날짜 기준의 게시글 ID 목록(set) 반환.
    """
    if isinstance(site_state, list):
        return set()

    daily = site_state.get("daily_snapshots", {})
    if not daily:
        return set()

    today      = datetime.now(KST).date()
    target_str = (today - timedelta(days=BASELINE_DAYS)).strftime("%Y-%m-%d")
    sorted_dates = sorted(daily.keys())

    baseline_date = None
    for d in sorted_dates:
        if d <= target_str:
            baseline_date = d

    if baseline_date is None:
        baseline_date = sorted_dates[0]

    log.info(f"  비교 기준 날짜: {baseline_date} (목표 7일 전: {target_str})")
    return set(daily[baseline_date])


def update_site_state(site_state, current_ids):
    """오늘 날짜의 게시글 목록을 저장하고, KEEP_DAYS 초과 데이터는 삭제."""
    if isinstance(site_state, list):
        site_state = {}

    today_str  = datetime.now(KST).strftime("%Y-%m-%d")
    cutoff_str = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")

    daily = site_state.get("daily_snapshots", {})
    daily[today_str] = current_ids[:100]   # 오늘 목록 저장 (EIASS 등 대비 100개 상향)

    site_state["daily_snapshots"] = {
        k: v for k, v in daily.items() if k >= cutoff_str
    }
    return site_state


# ── 스크래핑 ─────────────────────────────────────────────────────
def fetch_eiass(site):
    """EIASS POST API 직접 호출"""
    url = "https://www.eiass.go.kr/searchApi/search.do"
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': site["url"],
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    data = {
        'query': '풍력',
        'collection': 'business',
        'urlString': '&alias=2&completeFl=&openFl=&businessExquery=&whrChFl=&aSYear=&aEYear=&rSYear=&rEYear=&orgnCd=&nrvFl=&bizGubunCd=&perssGubn=M',
        'viewName': '/eiass/user/biz/base/info/searchListPer_searchApi',
        'currentPage': '1',
        'sort': 'DATE/DESC',
        'listCount': '100',
    }
    try:
        resp = requests.post(url, headers=headers, data=data, timeout=30, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('tbody tr')
        
        notices = []
        for r in rows:
            tds = r.find_all('td')
            if len(tds) >= 4:
                code_text = tds[0].get_text(strip=True)
                if code_text.startswith("ME") or len(code_text) == 10:
                    biz_code = code_text
                    biz_name = tds[2].get_text(strip=True)
                    date_rcv = tds[3].get_text(strip=True).replace('.', '-')
                else:
                    biz_code = "-"
                    biz_name = code_text
                    date_rcv = tds[2].get_text(strip=True).replace('.', '-') if len(tds) > 2 else ""

                notices.append({
                    "num": biz_code,
                    "title": biz_name,
                    "date": date_rcv,
                    "url": site["url"]
                })
        return notices, None
    except Exception as e:
        return None, str(e)


def fetch_notices(site, p_instance):
    if site.get("type") == "eiass":
        return fetch_eiass(site)

    try:
        browser = p_instance.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ko-KR",
        )
        page = context.new_page()
        page.goto(site["url"], wait_until="domcontentloaded", timeout=40000)

        if site.get("type") == "kepco":
            try: page.wait_for_selector('div[id*="notiRowGroup"]', timeout=20000)
            except: pass
        else:
            try: page.wait_for_selector("tbody tr", timeout=20000)
            except: pass

        html = page.content()
        browser.close()
    except Exception as e:
        log.error(f"[{site['name']}] 스크래핑 실패: {e}")
        return None, str(e)

    soup = BeautifulSoup(html, "html.parser")
    notices = []

    if site.get("type") == "kepco":
        rows = soup.select('div[id*="notiRowGroup"]')
        for row in rows:
            try:
                title_el = row.select_one('[id$="noticeTitle"]')
                date_el  = row.select_one('[id$="noticeRegDate"]')
                if title_el and date_el:
                    notices.append({
                        "num":   "-",
                        "title": title_el.get_text(strip=True),
                        "date":  date_el.get_text(strip=True),
                        "url":   site["url"],
                    })
            except:
                continue
    else:
        rows = soup.select("tbody tr")
        if len(rows) == 1 and "없습니다" in rows[0].get_text():
            return [], None
        for row in rows:
            tds = row.find_all("td")
            if len(tds) < max(site.get("title_idx", 0), site.get("date_idx", 0)) + 1:
                continue
            try:
                num      = tds[site["num_idx"]].get_text(strip=True)
                title_td = tds[site["title_idx"]]
                title_a  = title_td.find("a")
                title    = title_a.get_text(strip=True) if title_a else title_td.get_text(strip=True)
                date     = tds[site["date_idx"]].get_text(strip=True)
                if title:
                    notices.append({"num": num, "title": title, "date": date, "url": site["url"]})
            except:
                continue

    log.info(f"[{site['name']}] {len(notices)}건 파싱 완료")
    return notices, None


# ── 메인 ─────────────────────────────────────────────────────────
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    state = load_state()

    results = {
        "checked_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "sites": [],
        "is_updating": False,
    }

    with sync_playwright() as p:
        for site in SITES:
            log.info(f"[{site['name']}] 데이터 수집 시작")
            current, err = fetch_notices(site, p)

            if err or current is None:
                results["sites"].append({
                    "id": site["id"], "name": site["name"],
                    "icon": site["icon"], "color": site["color"],
                    "url": site["url"], "error": err or "데이터 수집 실패",
                    "new_count": 0, "new_items": [], "total": 0,
                })
            else:
                site_state   = state.get(site["id"], {})
                baseline_ids = get_baseline_ids(site_state)   # 7일 전 목록
                current_ids  = [item_id(n) for n in current]

                # 기준 목록이 있을 때만 비교, 없으면 새 글 = 0 (첫 실행 시 기준값 설정)
                new_items = [n for n in current if item_id(n) not in baseline_ids] \
                            if baseline_ids else []

                # 오늘 목록 저장 및 오래된 데이터 정리
                state[site["id"]] = update_site_state(site_state, current_ids)

                results["sites"].append({
                    "id": site["id"], "name": site["name"],
                    "icon": site["icon"], "color": site["color"],
                    "url": site["url"], "error": None,
                    "new_count": len(new_items),
                    "new_items": new_items[:10],
                    "total": len(current),
                })
                log.info(f"[{site['name']}] 신규(7일 기준): {len(new_items)}건 / 전체: {len(current)}건")

    save_state(state)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    log.info(f"✅ 저장 완료: {OUTPUT}")


if __name__ == "__main__":
    main()
