"""Optional offline browser check using synthetic reports and local static files."""
import argparse
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from news.publisher import build_snapshot


def main():
    from playwright.sync_api import sync_playwright
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', default='msedge')
    args = parser.parse_args()
    day = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *unused):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as pw:
            artifacts = {}
            for industry, label, field, kind in [('wind', '풍력', 'wind_type', '육상'), ('solar', '태양광', 'solar_type', '지붕형')]:
                issue = {'issue_id': industry + '-' + day, 'issue_date': day, 'revision': 1,
                         'approved': True, 'state': 'READY', 'published_at': day + 'T08:00:00+09:00',
                         'window_end': day + 'T07:30:00+09:00', 'content_status': 'normal',
                         'counts': {'published_topics': 1, 'held': 0}, 'coverage': {},
                         'headline_summary': [label + ' 시험용 브리핑'],
                         'items': [{'headline': label + ' 시험용 발전소 준공', 'summary': 'UI 검증을 위한 가상 기사입니다.',
                                    field: kind, 'companies': ['시험기업'], 'primary_category': '착공·준공·상업운전',
                                    'contract_stage': '미확인', 'source_url': 'https://example.kr/fixture',
                                    'source_name': '시험매체', 'source_published_at': day + 'T06:00:00+09:00',
                                    'event_id': 'fixture', 'representative_article_id': 'fixture'}]}
                snapshot = build_snapshot([issue], folder, industry)
                artifacts.update({path: (Path(folder) / path).read_bytes() for path in snapshot['files']})
            browser = pw.chromium.launch(channel=args.channel, headless=True)
            context = browser.new_context(viewport={'width': 1365, 'height': 1000})
            errors = []
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            state = {'fixtures': False, 'fail': False}
            def route(req):
                url = urlsplit(req.request.url)
                path = url.path.lstrip('/')
                if url.hostname != '127.0.0.1':
                    req.abort()
                elif state['fail'] and path == 'data/solar-news/latest.json':
                    req.fulfill(status=503, body='{}', content_type='application/json')
                elif state['fixtures'] and path in artifacts:
                    req.fulfill(body=artifacts[path], content_type='application/json')
                else:
                    req.continue_()
            context.route('**/*', route)
            base = 'http://127.0.0.1:' + str(server.server_port) + '/'
            page.goto(base + '#/solar')
            page.get_by_text('첫 브리핑 발간 준비 중', exact=True).wait_for()
            state['fixtures'] = True
            page.reload()
            page.locator('#solarContent h3', has_text='태양광 시험용 발전소').wait_for()
            page.locator('#siteMenu a[data-page="daily"]').click()
            page.locator('#dailyContent h3', has_text='풍력 시험용 발전소').wait_for()
            assert not page.locator('#solarPage').is_visible()
            page.goto(base + '#/solar/archive?period=all&solar=지붕형&q=태양광')
            page.locator('#solarContent .wn-article h3', has_text='태양광 시험용 발전소').wait_for()
            page.locator('#solarContent a', has_text='해당 호 보기').click()
            page.locator('#solarContent h2', has_text=day).wait_for()
            assert '/#/solar/' + day in page.url
            page.reload()
            page.locator('#solarContent h3', has_text='태양광 시험용 발전소').wait_for()
            page.goto(base + '#/solar')
            state['fail'] = True
            page.locator('#solarRefresh').click()
            page.get_by_text('최신 호 조회 실패', exact=True).wait_for()
            state['fail'] = False
            page.locator('#solarContent button', has_text='다시 시도').click()
            page.locator('#solarContent h3', has_text='태양광 시험용 발전소').wait_for()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#menuToggle').click()
            page.locator('#siteMenu a[data-page="solar"]').click()
            page.locator('#solarContent h3', has_text='태양광 시험용 발전소').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            destination = ROOT / '.news-runtime' / 'solar-mobile-preview.png'
            destination.parent.mkdir(exist_ok=True)
            page.screenshot(path=str(destination), full_page=True)
            assert not errors, errors
            browser.close()
            print('PASS: empty state, solar/wind tabs, archive filters, dated deep link, refresh recovery, mobile layout; no page errors')
            print('Synthetic fixture screenshot: ' + str(destination))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
