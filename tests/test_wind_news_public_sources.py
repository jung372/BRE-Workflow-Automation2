import unittest
from datetime import datetime, timezone

from news.public_sources import PublicCollector, SourceError, parse_article


SOURCE = {"source_id": "paper", "name": "신문", "hosts": ["news.example.com"],
          "discovery": "rss", "feed_urls": ["https://news.example.com/rss.xml"],
          "body_selector": "#article-view-content-div"}


def page(body=None):
    body = body or ("국내 해상풍력 단지의 터빈 설치가 완료됐다. " * 8)
    return ('<meta property="og:title" content="해상풍력 설치 완료">'
            '<meta property="article:published_time" content="2026-09-30T06:00:00+09:00">'
            '<article id="article-view-content-div">' + body + '</article>').encode()


def feed():
    return ('<rss><channel><item><title>풍력 설치</title>'
            '<link>https://news.example.com/article/1</link>'
            '<pubDate>Wed, 30 Sep 2026 06:00:00 +0900</pubDate>'
            '</item></channel></rss>').encode()


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
    def iter_content(self, size):
        yield self.data
    def close(self):
        pass


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class PublicSourceTests(unittest.TestCase):
    def collect(self, responses):
        transport = Transport(responses)
        collector = PublicCollector({"sources": [SOURCE]}, transport=transport, sleeper=lambda _: None)
        return collector.collect(until=datetime(2026, 9, 30, tzinfo=timezone.utc)), transport

    def test_no_access_identifier_required_for_public_body(self):
        result, transport = self.collect([Response(feed()), Response(page())])
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['articles'][0]['access_basis'], 'public_article_body')
        self.assertEqual(result['articles'][0]['access_status'], 'free')
        self.assertEqual(result['source_results'][0]['status'], 'success')
        self.assertFalse(transport.calls[0][1]['allow_redirects'])

    def test_visible_paywall_and_short_teaser_excluded(self):
        for body in ['로그인 후 기사를 읽을 수 있습니다. ' * 10,
                     '<div class="paywall">구독 전용</div>' + '안내' * 100,
                     '풍력 사업 안내']:
            with self.subTest(body=body[:20]), self.assertRaises(SourceError):
                parse_article(page(body), SOURCE, 'https://news.example.com/article/1')

    def test_subscription_navigation_is_not_article_paywall(self):
        item = parse_article('<nav>구독 후 기사를 읽을 수 있습니다</nav>'.encode() + page(), SOURCE,
                             'https://news.example.com/article/1')
        self.assertEqual(item['access_status'], 'free')

    def test_unsafe_redirect_rejected_before_second_request(self):
        result, transport = self.collect([Response(b'', 302, {'Location': 'https://127.0.0.1/private'})])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result['source_results'][0]['status'], 'request_error')

    def test_failed_parse_is_not_zero_news(self):
        result, _ = self.collect([Response(b'<html>try again</html>')])
        self.assertNotEqual(result['source_results'][0]['status'], 'success_zero')
        result, _ = self.collect([Response(b'<rss><channel/></rss>')])
        self.assertEqual(result['source_results'][0]['status'], 'success_zero')

    def test_article_failure_retains_other_source_data(self):
        transport = Transport([Response(feed()), Response(page()), Response(b'', 503)])
        config = {'sources': [SOURCE, dict(SOURCE, source_id='other')]}
        result = PublicCollector(config, transport, sleeper=lambda _: None).collect(
            until=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['source_results'][1]['status'], 'request_error')

    def test_oversize_response_rejected(self):
        result, _ = self.collect([Response(b'x' * 2_000_001)])
        self.assertEqual(result['source_results'][0]['error_code'], 'SOURCE_RESPONSE_TOO_LARGE')

    def test_list_follows_actual_pagination_and_deduplicates_links(self):
        source = dict(SOURCE, discovery='ndsoft_list', list_url='https://news.example.com/news/articleList.html?view_type=sm')
        html = ('<section id="section-list"><a href="/news/articleView.html?idxno=1">해상풍력</a>'
                '<a href="/news/articleView.html?idxno=1">풍력 설명</a></section>'
                '<a href="/news/articleList.html?page=2&amp;total=100">2</a>').encode()
        html2 = '<section id="section-list">기사가 없습니다</section>'.encode()
        transport = Transport([Response(html), Response(html2), Response(page())])
        result = PublicCollector({'sources': [source]}, transport, sleeper=lambda _: None).collect(
            until=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(transport.calls[1][0], 'https://news.example.com/news/articleList.html?page=2&total=100')
        self.assertEqual(len(result['articles']), 1)
        self.assertEqual(result['source_results'][0]['status'], 'success')

    def test_repeated_list_is_partial_coverage(self):
        source = dict(SOURCE, discovery='ndsoft_list', list_url='https://news.example.com/news/articleList.html')
        html = ('<section id="section-list"><a href="/news/articleView.html?idxno=1">해상풍력</a></section>'
                '<a href="/news/articleList.html?page=2">2</a>').encode()
        transport = Transport([Response(html), Response(html), Response(page())])
        result = PublicCollector({'sources': [source]}, transport, sleeper=lambda _: None).collect(
            until=datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(result['source_results'][0]['status'], 'saturated')
        self.assertEqual(len(result['articles']), 1)


if __name__ == '__main__':
    unittest.main()
