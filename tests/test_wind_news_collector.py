import unittest
from datetime import datetime, timezone

from news.collector import Collector, safe_public_url


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def article(**overrides):
    return dict({"title": "<b>해상풍력</b> 사업", "description": "100MW &amp; 터빈 공급",
        "originallink": "https://news.example.com/article/1", "link": "https://n.news.naver.com/article/1",
        "pubDate": "Wed, 30 Sep 2026 06:00:00 +0900"}, **overrides)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.config = {"enabled": True, "queries": ["풍력"], "page_size": 2, "max_pages": 3,
            "sources": [{"source_id": "example", "name": "Example", "hosts": ["news.example.com"], "required": True}]}
        self.end = datetime(2026, 9, 30, tzinfo=timezone.utc)

    def test_original_link_description_and_naver_time(self):
        transport = Transport([Response({"items": [article()], "total": 1})])
        result = Collector(self.config, transport).collect(until=self.end)
        item = result["articles"][0]
        self.assertEqual(item["url"], "https://news.example.com/article/1")
        self.assertEqual(item["description"], "100MW & 터빈 공급")
        self.assertEqual(item["evidence_scope"], "description")
        self.assertEqual(item["published_at_basis"], "naver_pubDate")
        self.assertEqual(result["source_results"][0]["status"], "success")
        self.assertFalse(transport.calls[0][1]["allow_redirects"])
        self.assertEqual(transport.calls[0][1]["params"]["sort"], "date")

    def test_overlap_pagination_and_deduplication(self):
        recent = article()
        overlap = article(originallink="https://news.example.com/article/2", pubDate="Mon, 28 Sep 2026 06:00:00 +0900")
        older = article(originallink="https://news.example.com/article/3", pubDate="Fri, 25 Sep 2026 06:00:00 +0900")
        transport = Transport([Response({"items": [recent, recent], "total": 4}), Response({"items": [overlap, older], "total": 4})])
        result = Collector(self.config, transport).collect(since=self.end, until=self.end)
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual([call[1]["params"]["start"] for call in transport.calls], [1, 3])

    def test_zero_is_distinct_from_auth_request_parse_and_partial(self):
        cases = [(Response({"items": [], "total": 0}), "success_zero"),
                 (Response({}, 401), "auth_error"), (Response({}, 500), "request_error"),
                 (Response({"items": "bad", "total": 1}), "parse_error")]
        for response, expected in cases:
            with self.subTest(expected=expected):
                result = Collector(self.config, Transport([response])).collect(until=self.end)
                self.assertEqual(result["source_results"][0]["status"], expected)
        invalid = article(pubDate="not a date")
        result = Collector(self.config, Transport([Response({"items": [article(), invalid], "total": 2})])).collect(until=self.end)
        self.assertEqual(result["source_results"][0]["status"], "partial")
        self.assertEqual(len(result["articles"]), 1)

    def test_saturation_does_not_claim_complete_collection(self):
        self.config["max_pages"] = 1
        result = Collector(self.config, Transport([Response({"items": [article(), article()], "total": 9000})])).collect(until=self.end)
        self.assertEqual(result["source_results"][0]["status"], "saturated")

    def test_redirect_or_private_original_never_uses_fallback(self):
        for url in ["http://news.example.com/a", "https://127.0.0.1/a", "https://news.example.com.evil.test/a"]:
            transport = Transport([Response({"items": [article(originallink=url)], "total": 1})])
            self.assertEqual(Collector(self.config, transport).collect(until=self.end)["articles"], [])
        result = Collector(self.config, Transport([Response({}, 302)])).collect(until=self.end)
        self.assertEqual(result["source_results"][0]["status"], "request_error")

    def test_disabled_never_calls_transport_or_reads_credentials(self):
        self.config["enabled"] = False
        result = Collector(self.config, Transport([])).collect()
        self.assertEqual(result["source_results"][0]["status"], "disabled")

    def test_url_boundary_rejects_credentials_ports_and_private_dns(self):
        for url in ["https://u:p@news.example.com/a", "https://news.example.com:444/a", "https://localhost/a"]:
            with self.assertRaises(ValueError):
                safe_public_url(url, ["news.example.com", "localhost"])
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("10.0.0.1", 443))]
        with self.assertRaises(ValueError):
            safe_public_url("https://news.example.com/a", ["news.example.com"], resolve=True, resolver=resolver)

    def test_live_naver_is_blocked_even_when_collection_enabled(self):
        from unittest.mock import patch
        with patch("news.collector.os.environ.get", side_effect=AssertionError("Must not read credentials")):
            result = Collector(self.config).collect(until=self.end)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["source_results"][0]["error_code"], "NAVER_TERMS_INCOMPATIBLE")


if __name__ == "__main__":
    unittest.main()
