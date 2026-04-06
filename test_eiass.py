import requests
from bs4 import BeautifulSoup
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

url = "https://www.eiass.go.kr/searchApi/search.do"
headers = {
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'X-Requested-With': 'XMLHttpRequest',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}
data = {
    'query': '풍력',
    'collection': 'business',
    'urlString': '&alias=2&completeFl=&openFl=&businessExquery=&whrChFl=&aSYear=&aEYear=&rSYear=&rEYear=&orgnCd=&nrvFl=&bizGubunCd=&perssGubn=M',
    'viewName': '/eiass/user/biz/base/info/searchListPer_searchApi',
    'currentPage': '1',
    'sort': 'DATE/DESC',
    'listCount': '10',
}
resp = requests.post(url, headers=headers, data=data, timeout=30, verify=False)
soup = BeautifulSoup(resp.text, 'html.parser')
rows = soup.select('tbody tr')
print(f"Num rows: {len(rows)}")
for r in rows:
    tds = r.find_all('td')
    if len(tds) >= 4:
        print(f"[{tds[0].get_text(strip=True)}] {tds[2].get_text(strip=True)} ({tds[3].get_text(strip=True)})")
