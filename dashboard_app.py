"""
주요 사이트 공지 모니터링 대시보드 서버 (백그라운드 업데이트 방식)
실행: python dashboard_app.py
접속: http://localhost:5000
"""

from flask import Flask, jsonify, send_from_directory
import json
import os
import logging
from datetime import datetime
import threading
import time
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "last_state.json")

app = Flask(__name__, static_folder=BASE_DIR)
log = logging.getLogger("korec_dashboard")

# ──────────────────────────────────────────────
# 사이트 정의
# ──────────────────────────────────────────────
SITES = [
    {
        "id":    "notice",
        "name":  "전기위 공지사항",
        "url":   "https://www.korec.go.kr/notice/selectNoticeList.do",
        "icon":  "📢",
        "color": "#3b82f6",
        "title_idx": 2, "date_idx": 3, "num_idx": 0,
    },
    {
        "id":    "result",
        "name":  "위원회 개최결과",
        "url":   "https://www.korec.go.kr/notice/result/selectNoticeList.do",
        "icon":  "📋",
        "color": "#10b981",
        "title_idx": 1, "date_idx": 2, "num_idx": 0,
    },
    {
        "id":    "nie_notice",
        "name":  "생태.자연도 공고",
        "url":   "https://www.nie.re.kr/nie/bbs/BMSR00038/list.do?menuNo=200099&pageIndex=1&gubunCd=&searchCondition=&searchKeyword=",
        "icon":  "🍃",
        "color": "#eab308",
        "title_idx": 1, "date_idx": 4, "num_idx": 0,
    },
    {
        "id":    "eiass_wind",
        "name":  "소규모 환평(풍력)",
        "url":   "https://www.eiass.go.kr/biz/base/info/perList.do?menu=biz&biz_gubn=M",
        "icon":  "🌬️",
        "color": "#0ea5e9",
        "type":  "eiass",
    },
    {
        "id":    "kepco_notice",
        "name":  "한전 설계포털 공지",
        "url":   "https://online.kepco.co.kr/EWM040D00",
        "icon":  "⚡",
        "color": "#f97316",
        "type":  "kepco",
    },
]

METMASTS = [
    {"id": "SIRU", "name": "SIRU", "env_prefix": "METMAST_SIRU", "url": os.environ.get("METMAST_SIRU_URL", "https://D225107.connect.ammonit.com/")},
    {"id": "GOGK", "name": "GOGK", "env_prefix": "METMAST_GOGK", "url": os.environ.get("METMAST_GOGK_URL", "https://D243097.connect.ammonit.com/")},
    {"id": "BLMU", "name": "BLMU", "env_prefix": "METMAST_BLMU", "url": os.environ.get("METMAST_BLMU_URL", "")},
    {"id": "DKAM", "name": "DKAM", "env_prefix": "METMAST_DKAM", "url": os.environ.get("METMAST_DKAM_URL", "")}
]

# 전역 데이터 상태 초기화
_latest_data = {
    "checked_at": "-", 
    "sites": [
        {"id": s["id"], "name": s["name"], "icon": s["icon"], "color": s["color"], 
         "url": s["url"], "error": None, "new_count": 0, "new_items": [], "total": 0} 
        for s in SITES
    ], 
    "metmasts": [
        {"id": m["id"], "name": m["name"], "status": "Offline"}
        for m in METMASTS
    ],
    "is_updating": True
}
_data_lock = threading.Lock()

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except: return {}
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except: pass

def fetch_notices(site, p_instance):
    html = ""
    try:
        browser = p_instance.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ko-KR",
        )
        page = context.new_page()
        page.goto(site["url"], wait_until="domcontentloaded", timeout=40000)
        
        # 사이트 유형별 대기 및 파싱
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
        # 한전 전용 파서 (WebSquare 레이아웃)
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
                        "url":   site["url"]
                    })
            except: continue
    else:
        # 일반 테이블 파서
        rows = soup.select("tbody tr")
        if len(rows) == 1 and ("없습니다" in rows[0].get_text()):
            return [], None

        for row in rows:
            tds = row.find_all("td")
            if len(tds) < max(site.get("title_idx", 0), site.get("date_idx", 0)) + 1: continue
            try:
                num   = tds[site["num_idx"]].get_text(strip=True)
                title_td = tds[site["title_idx"]]
                title_a  = title_td.find("a")
                title    = title_a.get_text(strip=True) if title_a else title_td.get_text(strip=True)
                date     = tds[site["date_idx"]].get_text(strip=True)
                if title:
                    notices.append({"num": num, "title": title, "date": date, "url": site["url"]})
            except: continue

    log.info(f"[{site['name']}] {len(notices)}건 파싱 완료")
    return notices, None

def check_metmast(m, p_instance):
    url = m["url"]
    user = os.environ.get(f"{m['env_prefix']}_ID", "")
    pw = os.environ.get(f"{m['env_prefix']}_PW", "")
    if not url or not user or not pw:
        return {"id": m["id"], "name": m["name"], "status": "Offline"}
    try:
        browser = p_instance.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(url, timeout=40000, wait_until="load")
        
        # 1. Access Code
        access_sel = 'input[name="access"], input[type="password"]'
        try:
            page.wait_for_selector(access_sel, timeout=10000)
            page.fill(access_sel, 'Ammonit')
            page.keyboard.press("Enter")
            page.wait_for_load_state("load")
        except: pass
            
        # 2. Login
        user_sel = 'input[name="user"], input[name*="login"]'
        try:
            page.wait_for_selector(user_sel, timeout=10000)
            page.fill(user_sel, user)
            page.fill('input[type="password"]', pw)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(3000)
        except: pass
            
        try: content = page.content()
        except: content = ""
        browser.close()
        
        if any(kw in content for kw in ["Welcome", "Meteo-40", "Logout", "Dashboard"]):
            return {"id": m["id"], "name": m["name"], "status": "Online"}
        return {"id": m["id"], "name": m["name"], "status": "Offline"}
    except Exception as e:
        log.error(f"  [{m['name']}] 체크 오류: {e}")
        return {"id": m["id"], "name": m["name"], "status": "Offline"}

def update_data_task():
    global _latest_data
    while True:
        try:
            log.info("데이터 업데이트 시작 (백그라운드)")
            with _data_lock:
                _latest_data["is_updating"] = True
                
            new_results = {
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sites": [],
                "metmasts": [],
                "is_updating": False
            }
            state = load_state()
            
            with sync_playwright() as p:
                for m in METMASTS:
                    log.info(f"[MetMast] {m['name']} 확인 중...")
                    status = check_metmast(m, p)
                    new_results["metmasts"].append(status)
                
                for site in SITES:
                    current, err = fetch_notices(site, p)
                    if err or current is None:
                        new_results["sites"].append({
                            "id": site["id"], "name": site["name"], "icon": site["icon"], "color": site["color"],
                            "url": site["url"], "error": err or "실패", "new_count": 0, "new_items": [], "total": 0
                        })
                    else:
                        def item_id(n): return f"{n['title']}||{n['date']}"
                        prev_ids = set(state.get(site["id"], []))
                        current_ids = [item_id(n) for n in current]
                        new_items = [n for n in current if item_id(n) not in prev_ids] if prev_ids else []
                        state[site["id"]] = current_ids[:30]
                        new_results["sites"].append({
                            "id": site["id"], "name": site["name"], "icon": site["icon"], "color": site["color"],
                            "url": site["url"], "error": None, "new_count": len(new_items), "new_items": new_items[:10], "total": len(current)
                        })
            save_state(state)
            with _data_lock: _latest_data = new_results
                
        except Exception as e:
            log.error(f"백그라운드 작업 중 오류: {e}")
            with _data_lock: _latest_data["is_updating"] = False
        
        log.info("데이터 업데이트 완료. 10분 대기...")
        time.sleep(600)

@app.route("/api/status")
def api_status():
    with _data_lock: return jsonify(_latest_data)

@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    return jsonify({"ok": True})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
    return jsonify({"ok": True})

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "dashboard.html")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
    threading.Thread(target=update_data_task, daemon=True).start()
    
    print("=" * 50)
    print("  주요 사이트 공지 모니터링 대시보드")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
