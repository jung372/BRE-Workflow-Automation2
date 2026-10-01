import json
import unittest
from datetime import datetime, timezone

from news.public_sources import SourceError, parse_article
from news.search_sources import SearchCollector, publisher_url, search_page
from news.relevance import industry_relevant, editorial_order
from tests.test_wind_news_public_sources import Response, Transport

SOURCE = {"source_id":"search", "name":"검색", "discovery":"naver_news_search"}
NEXT = "https://s.search.naver.com/p/newssearch/3/api/tab/more?query=x&start=11"


class IndustrySearchTests(unittest.TestCase):
    def test_current_and_legacy_search_links_and_next_url(self):
        text = '<a data-heatmap-target=".tit" href="http://paper.kr/news/1?utm_source=naver">제목</a>'
        text += '<a class="news_tit" href="https://paper.kr/news/1">중복</a>'
        text += '<script>{"url":'+json.dumps(NEXT)+',"insertTarget":"._infinite_list"}</script>'
        links, next_url = search_page(text)
        self.assertEqual(links, ["https://paper.kr/news/1"])
        self.assertEqual(next_url, NEXT)
        links, next_url = search_page(json.dumps({"collection":[{"html":'<a data-heatmap-target=".tit" href="https://other.kr/2">기사</a>'}],"url":None}))
        self.assertEqual(links,["https://other.kr/2"])
        self.assertIsNone(next_url)

    def test_invalid_page_not_zero_and_unsafe_next_rejected(self):
        for page in ['<html>access denied</html>',
                     '<script>{"url":"https://127.0.0.1/private","insertTarget":"x"}</script>']:
            with self.assertRaises((SourceError,ValueError)): search_page(page)
        self.assertEqual(search_page('<p>검색결과가 없습니다</p>'),([],None))

    def test_untrusted_destination_and_credentials_rejected(self):
        for url in ['https://127.0.0.1/', 'https://localhost/', 'https://x.internal/',
                    'https://user:secret@paper.kr/a', 'https://paper.kr:8444/',
                    'https://news.naver.com/a', 'file:///tmp/a']:
            with self.subTest(url=url), self.assertRaises(ValueError): publisher_url(url)
        collector = SearchCollector({}, Transport([Response(b'',302,{'Location':'https://192.168.1.1/'})]), sleeper=lambda _:None)
        with self.assertRaises(ValueError): collector.fetch_publisher('https://paper.kr/a')
        self.assertEqual(len(collector.transport.calls),1)

    def test_publisher_body_and_time_are_required_not_search_snippets(self):
        body = '국내 해상 에너지 단지에 하부구조물을 공급한다. ' * 8
        page = '<meta property="og:title" content="하부구조물 공급 확대"><meta property="og:site_name" content="다른신문">'
        page += '<header class="article-view-header">승인 2026.09.30 12:30</header><div id="article-view-content-div">'+body+'</div>'
        article = parse_article(page, SOURCE, 'https://paper.kr/a')
        self.assertEqual(article['source_name'],'다른신문')
        self.assertEqual(article['source_published_at'],'2026-09-30T12:30:00+09:00')
        with self.assertRaises(SourceError): parse_article(page.replace('승인 2026.09.30 12:30',''),SOURCE,'https://paper.kr/a')
        with self.assertRaises(SourceError): parse_article('<meta property="og:title" content="풍력">검색 발췌문만 있음',SOURCE,'https://paper.kr/a')

    def test_paywall_jsonld_and_visible_lock_never_bypass(self):
        page = '<meta property="og:title" content="풍력"><div id="articleBody">'+('풍력 사업 소식입니다. '*20)+'</div>'
        page += '<script type="application/ld+json">'+json.dumps({'@type':'NewsArticle','isAccessibleForFree':False,'datePublished':'2026-09-30T00:00:00Z'})+'</script>'
        with self.assertRaisesRegex(SourceError,'ACCESS_RESTRICTED'): parse_article(page,SOURCE,'https://paper.kr/a')

    def test_every_query_runs_before_second_page_and_keeps_successes(self):
        first = '<a class="news_tit" href="https://paper.kr/1">풍력</a><script>{"url":'+json.dumps(NEXT)+',"insertTarget":"x"}</script>'
        transport = Transport([Response(first.encode()),Response(b'',503),Response(json.dumps({'collection':[{'html':'<a class="news_tit" href="https://paper.kr/2">풍력</a>'}],'url':None}).encode())])
        collector = SearchCollector({'queries':['풍력','모노파일'],'search_pages_per_query':2},transport,sleeper=lambda _:None)
        collector.fetch_publisher = lambda url: (('<meta property="og:title" content="모노파일 공급"><meta property="article:published_time" content="2026-09-30T12:00:00Z"><div id="articleBody">'+('모노파일 공급 계약이다. '*20)+'</div>').encode(),url)
        result = collector.collect_source(SOURCE,datetime(2026,9,29,tzinfo=timezone.utc),datetime(2026,10,1,tzinfo=timezone.utc))
        self.assertEqual(len(result['articles']),2)
        self.assertEqual(result['source_results'][0]['status'],'partial')
        self.assertIn('%EB%AA%A8%EB%85%B8',transport.calls[1][0])
        self.assertEqual(transport.calls[2][0],NEXT)

    def test_industry_scope_without_wind_word_and_company_false_positive(self):
        self.assertTrue(industry_relevant('모노파일 수주'))
        self.assertTrue(industry_relevant('해저케이블 공장 증설','해상 에너지 사업에 납품한다'))
        self.assertFalse(industry_relevant('GS엔텍 임원 인사','기업 소식'))
        self.assertFalse(industry_relevant('가스터빈','화력 발전소 준공'))

    def test_summary_budget_prioritizes_industry_and_multiple_publishers(self):
        def article(title,name,url): return {'title':title,'source_name':name,'url':url,'source_published_at':'2026-09-30T00:00:00Z'}
        groups = {'old':[article('다른 산업 주가 동향','전기신문','1')],
                  'wind':[article('태안 해상풍력 모노파일 수주','신문A','2')],
                  'repeat':[article('태안 해상풍력 모노파일 수주 발표','신문A','3')],
                  'policy':[article('주민 참여 육상풍력 인허가 개선','신문B','4')]}
        ordered = editorial_order(groups)
        self.assertEqual({p[0] for p in ordered[:2]}, {'repeat','policy'})
        self.assertEqual(len(ordered),4)  # no uncertain event merging


if __name__ == '__main__': unittest.main()
