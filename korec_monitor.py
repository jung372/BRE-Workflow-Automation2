"""
KOREC 공지사항 자동 모니터링 & 이메일 알림 스크립트
=======================================================
모니터링 대상:
  - 공지사항:       https://www.korec.go.kr/notice/selectNoticeList.do
  - 위원회 개최결과: https://www.korec.go.kr/notice/result/selectNoticeList.do

실행 방법:
  python korec_monitor.py

자동 실행 (Windows 작업 스케줄러):
  run_korec_monitor.bat 을 작업 스케줄러에 등록하세요.
"""

import requests
from bs4 import BeautifulSoup
import smtplib
import json
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

# ──────────────────────────────────────────────
# ★ 설정 영역 (config.json 파일로 관리됩니다)
# ──────────────────────────────────────────────
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE  = os.path.join(os.path.dirname(__file__), "last_state.json")
LOG_FILE    = os.path.join(os.path.dirname(__file__), "monitor.log")

SITES = [
    {
        "name": "KOREC 공지사항",
        "url":  "https://www.korec.go.kr/notice/selectNoticeList.do",
        "title_selector": "tbody tr td:nth-child(3) a",
        "date_selector":  "tbody tr td:nth-child(4)",
        "num_selector":   "tbody tr td:nth-child(1)",
        "base_url":       "https://www.korec.go.kr",
    },
    {
        "name": "KOREC 위원회 개최결과",
        "url":  "https://www.korec.go.kr/notice/result/selectNoticeList.do",
        "title_selector": "tbody tr td:nth-child(2) a",
        "date_selector":  "tbody tr td:nth-child(3)",
        "num_selector":   "tbody tr td:nth-child(1)",
        "base_url":       "https://www.korec.go.kr",
    },
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 설정 로드
# ──────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        log.error(f"config.json 파일이 없습니다: {CONFIG_FILE}")
        log.error("config_sample.json 을 참고하여 config.json 을 작성하세요.")
        raise FileNotFoundError(CONFIG_FILE)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────
# 상태 파일 (마지막으로 확인한 게시글 ID 저장)
# ──────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# 페이지 크롤링
# ──────────────────────────────────────────────
def fetch_notices(site: dict) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    try:
        resp = requests.get(site["url"], headers=headers, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
    except Exception as e:
        log.error(f"[{site['name']}] 페이지 요청 실패: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("tbody tr")

    notices = []
    for row in rows:
        try:
            num_td    = row.select_one(site["num_selector"].split(" ")[-1])
            title_tag = row.select_one(site["title_selector"].split(" ")[-1])
            date_td   = row.select_one(site["date_selector"].split(" ")[-1])

            if not title_tag:
                continue

            num   = num_td.get_text(strip=True)   if num_td   else ""
            title = title_tag.get_text(strip=True) if title_tag else ""
            date  = date_td.get_text(strip=True)   if date_td  else ""
            href  = title_tag.get("href", "")

            # 링크가 상대경로인 경우 절대경로로 변환
            if href and not href.startswith("http"):
                href = site["base_url"] + href

            notices.append({"num": num, "title": title, "date": date, "url": href})
        except Exception as e:
            log.warning(f"[{site['name']}] 행 파싱 오류: {e}")
            continue

    log.info(f"[{site['name']}] {len(notices)}개 게시글 확인")
    return notices


# ──────────────────────────────────────────────
# 이메일 발송
# ──────────────────────────────────────────────
def send_email(cfg: dict, subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["sender_email"]
    msg["To"]      = ", ".join(cfg["recipient_emails"])

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["sender_email"], cfg["sender_password"])
            server.sendmail(
                cfg["sender_email"],
                cfg["recipient_emails"],
                msg.as_string(),
            )
        log.info(f"이메일 발송 완료: {subject}")
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


def build_email_html(site_name: str, new_items: list[dict], site_url: str) -> str:
    rows_html = ""
    for item in new_items:
        link = f'<a href="{item["url"]}">{item["title"]}</a>' if item["url"] else item["title"]
        rows_html += f"""
        <tr>
          <td style="padding:8px 12px; border-bottom:1px solid #eee; color:#666; width:60px; text-align:center;">{item['num']}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #eee;">{link}</td>
          <td style="padding:8px 12px; border-bottom:1px solid #eee; color:#888; width:100px; text-align:center;">{item['date']}</td>
        </tr>"""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""
    <html><body style="font-family: 'Noto Sans KR', Arial, sans-serif; background:#f7f8fa; margin:0; padding:20px;">
      <div style="max-width:680px; margin:auto; background:#fff; border-radius:10px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,0.08);">
        <div style="background:linear-gradient(135deg,#1a56db,#0e9f6e); padding:28px 32px;">
          <h1 style="color:#fff; margin:0; font-size:20px;">🔔 새 게시글 알림</h1>
          <p style="color:rgba(255,255,255,0.85); margin:6px 0 0; font-size:14px;">{site_name}</p>
        </div>
        <div style="padding:24px 32px;">
          <p style="color:#374151; margin:0 0 16px;">아래 <strong>{len(new_items)}건</strong>의 새 게시글이 등록되었습니다. <span style="color:#9ca3af; font-size:13px;">({now_str} 확인)</span></p>
          <table style="width:100%; border-collapse:collapse; font-size:14px;">
            <thead>
              <tr style="background:#f3f4f6;">
                <th style="padding:10px 12px; text-align:center; color:#6b7280; font-weight:600; width:60px;">번호</th>
                <th style="padding:10px 12px; text-align:left; color:#6b7280; font-weight:600;">제목</th>
                <th style="padding:10px 12px; text-align:center; color:#6b7280; font-weight:600; width:100px;">등록일</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
          <div style="margin-top:20px; text-align:right;">
            <a href="{site_url}" style="display:inline-block; background:#1a56db; color:#fff; text-decoration:none; padding:10px 20px; border-radius:6px; font-size:14px;">사이트 바로가기 →</a>
          </div>
        </div>
        <div style="background:#f9fafb; padding:14px 32px; font-size:12px; color:#9ca3af; border-top:1px solid #f3f4f6;">
          이 메일은 KOREC 모니터링 스크립트가 자동 발송하였습니다.
        </div>
      </div>
    </body></html>"""


# ──────────────────────────────────────────────
# 메인 로직
# ──────────────────────────────────────────────
def main():
    log.info("=" * 50)
    log.info("KOREC 공지사항 모니터링 시작")

    cfg   = load_config()
    state = load_state()
    any_new = False

    for site in SITES:
        site_key = site["name"]
        notices  = fetch_notices(site)
        if not notices:
            continue

        # 가장 최근 게시글의 (번호, 제목) 조합으로 신규 여부 판단
        prev_ids = set(state.get(site_key, []))

        # 고유 ID = "번호||제목"
        def item_id(n):
            return f"{n['title']}||{n['date']}"

        new_items = [n for n in notices if item_id(n) not in prev_ids]
        current_ids = [item_id(n) for n in notices]

        if new_items and prev_ids:
            # 이전 상태가 존재할 때만 알림 (첫 실행 시에는 기준 저장만)
            log.info(f"[{site_key}] 신규 게시글 {len(new_items)}건 발견!")
            subject  = f"[KOREC 알림] {site_key}에 새 게시글이 {len(new_items)}건 등록되었습니다"
            html     = build_email_html(site_key, new_items, site["url"])
            send_email(cfg, subject, html)
            any_new = True
        elif not prev_ids:
            log.info(f"[{site_key}] 첫 실행: 현재 게시글 {len(notices)}건을 기준으로 저장합니다.")
        else:
            log.info(f"[{site_key}] 새 게시글 없음.")

        # 상태 업데이트 (최신 30개만 유지)
        state[site_key] = current_ids[:30]

    save_state(state)

    if not any_new:
        log.info("새 게시글 없음. 이메일 발송 없음.")

    log.info("모니터링 완료")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
