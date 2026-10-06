import unittest
from unittest.mock import Mock

import requests

from news.delivery import build_card, send_card


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.issue = {"issue_date": "2026-09-30", "revision": 1, "items": [
            {"headline": "풍력 EPC", "summary": "기사 근거 요약", "source_name": "시험일보",
             "source_url": f"https://example.kr/news?id={i}"} for i in range(8)]}
        self.site = "https://jung372.github.io/BRE-Workflow-Automation2/"
        self.webhook = "https://example.logic.azure.com/workflows/test?sig=SECRET"

    def test_card_has_all_linked_titles_and_publishers_without_summaries(self):
        card = build_card(self.issue, self.site)["attachments"][0]["content"]
        self.assertEqual(len(card["body"]), 10)
        self.assertIn("[풍력 EPC](https://example.kr/news?id=7) — 시험일보", card["body"][-1]["text"])
        self.assertNotIn("기사 근거 요약", str(card))
        self.assertTrue(card["actions"][0]["url"].endswith("/#/daily/2026-09-30"))

    def test_http_accepted_is_not_delivery(self):
        client = Mock()
        client.post.return_value.status_code = 202
        self.assertEqual(send_card(self.issue, self.webhook, self.site, client)["status"], "ACCEPTED")
        self.assertFalse(client.post.call_args.kwargs["allow_redirects"])

    def test_timeout_is_unknown_and_redacted(self):
        client = Mock()
        client.post.side_effect = requests.Timeout(self.webhook)
        result = send_card(self.issue, self.webhook, self.site, client)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("SECRET", str(result))

    def test_server_error_is_not_safe_to_retry(self):
        client = Mock()
        client.post.return_value.status_code = 500
        self.assertEqual(send_card(self.issue, self.webhook, self.site, client)["status"], "UNKNOWN")

    def test_explicit_rejection_is_failed(self):
        client = Mock()
        client.post.return_value.status_code = 429
        self.assertEqual(send_card(self.issue, self.webhook, self.site, client)["status"], "FAILED")

    def test_unconfigured_does_not_send(self):
        client = Mock()
        self.assertEqual(send_card(self.issue, "", self.site, client)["status"], "SKIPPED")
        client.post.assert_not_called()

    def test_private_and_lookalike_hosts_rejected(self):
        for url in ["http://localhost/", "https://127.0.0.1/", "https://evil-logic.azure.com/",
                    "https://good.logic.azure.com.evil.example/", "https://user:pass@a.logic.azure.com/"]:
            with self.subTest(url=url):
                client = Mock()
                self.assertEqual(send_card(self.issue, url, self.site, client)["status"], "FAILED")
                client.post.assert_not_called()

    def test_text_markdown_and_invalid_dates(self):
        self.issue["items"][0]["headline"] = "[click](https://evil.example)"
        self.issue["items"][0]["source_url"] = "https://example.kr/(story)?a=1&b=2"
        card = build_card(self.issue, self.site)["attachments"][0]["content"]
        self.assertIn("\\[click\\]", card["body"][2]["text"])
        self.assertIn("https://example.kr/%28story%29?a=1&b=2", card["body"][2]["text"])
        self.issue["issue_date"] = "../../secrets"
        with self.assertRaises(ValueError):
            build_card(self.issue, self.site)

    def test_no_news_and_partial_reports_still_have_web_link(self):
        self.issue.update(items=[], content_status="partial")
        card = build_card(self.issue, self.site)["attachments"][0]["content"]
        self.assertIn("일부 수집", str(card))
        self.assertIn("주요 기사가 없습니다", str(card))
        self.assertEqual(len(card["actions"]), 1)

    def test_invalid_article_link_never_contacts_teams(self):
        self.issue["items"][0]["source_url"] = "javascript:alert(1)"
        client = Mock()
        self.assertEqual(send_card(self.issue, self.webhook, self.site, client)["status"], "FAILED")
        client.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
