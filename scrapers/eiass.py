import re, logging, requests, urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

_BIZ_CODE = re.compile(r"^[A-Z]{2}\d{8}$")


def fetch_eiass(site: dict) -> tuple:
    """EIASS POST API 직접 호출 (사업코드 패턴 필터링)."""
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": site["url"],
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    data = {
        "query": "풍력", "collection": "business",
        "urlString": "&alias=2&completeFl=&openFl=&businessExquery=&whrChFl=&aSYear=&aEYear=&rSYear=&rEYear=&orgnCd=&nrvFl=&bizGubunCd=&perssGubn=M",
        "viewName": "/eiass/user/biz/base/info/searchListPer_searchApi",
        "currentPage": "1", "sort": "DATE/DESC", "listCount": "100",
    }
    try:
        resp = requests.post(
            "https://www.eiass.go.kr/searchApi/search.do",
            headers=headers, data=data, timeout=30, verify=False,
        )
        resp.raise_for_status()
        soup    = BeautifulSoup(resp.text, "html.parser")
        notices = []
        for r in soup.select("tbody tr"):
            tds = r.find_all("td")
            if len(tds) < 4 or not _BIZ_CODE.match(tds[0].get_text(strip=True)):
                continue
            notices.append({
                "num":       tds[0].get_text(strip=True),
                "title":     tds[2].get_text(strip=True),
                "date":      tds[3].get_text(strip=True).replace(".", "-"),
                "comp_date": tds[4].get_text(strip=True).replace(".", "-") if len(tds) > 4 else "",
                "status":    tds[5].get_text(strip=True) if len(tds) > 5 else "",
                "url":       site["url"],
            })
        # 동일 사업코드(num)의 복수 행을 가장 최신 date 기준으로 1개만 유지
        seen: dict = {}
        for r in notices:
            n = r["num"]
            if n not in seen or r["date"] > seen[n]["date"]:
                seen[n] = r
        notices = list(seen.values())

        log.info(f"[{site['name']}] {len(notices)}건 파싱 완료")
        return notices, None
    except Exception as e:
        return None, str(e)
