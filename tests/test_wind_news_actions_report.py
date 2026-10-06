from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock

from scripts.wind_news.report_briefing import report


class ActionsReportTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.session.get.return_value.json.return_value = {"teams_configured": True}
        self.session.post.return_value.status_code = 200

    def test_preview_only_builds_card_and_does_not_send(self):
        self.session.post.return_value.json.return_value = {"attachments": [{"content": {
            "body": [{"text": "title"}, {"text": "[기사](https://example.kr/) — 매체"}]}}]}
        result, code = report(self.session, "preview", "2026-10-06")
        self.assertEqual(code, 0)
        self.assertEqual(result["article_count"], 1)
        self.assertEqual(result["status"], "PREVIEWED")
        self.assertTrue(self.session.post.call_args.args[0].endswith("/preview"))

    def test_scheduled_uses_seoul_day_and_server_daily_guard(self):
        self.session.post.return_value.json.return_value = {"status": "ACCEPTED"}
        clock = lambda: datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)
        result, code = report(self.session, "scheduled", clock=clock)
        self.assertEqual(result["issue_date"], "2026-10-06")
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.post.call_args.kwargs["json"], {
            "issue_date": "2026-10-06", "scheduled": True, "message_type": "daily", "channel_id": "wind-news"})

    def test_missing_teams_is_visible_skip_and_deadline_is_failure(self):
        for response, expected in [
            ({"status": "SKIPPED", "error_code": "TEAMS_NOT_CONFIGURED"}, 0),
            ({"status": "SKIPPED", "error_code": "PUBLICATION_NOT_READY_BY_DEADLINE"}, 1),
            ({"status": "UNKNOWN", "error_code": "TIMEOUT"}, 1),
        ]:
            self.session.post.return_value.json.return_value = response
            result, code = report(self.session, "send", "2026-10-06")
            self.assertEqual(code, expected)
            self.assertEqual(result["status"], response["status"])

    def test_bad_inputs_do_not_contact_service(self):
        for mode, day in [("force", "2026-10-06"), ("send", "2026-02-30"), ("scheduled", "2020-01-01")]:
            with self.assertRaises(ValueError):
                report(self.session, mode, day)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_arbitrary_service_errors_never_enter_action_output(self):
        self.session.post.return_value.json.return_value = {"status": "FAILED", "error_code": "https://fixture/?sig=SECRET"}
        result, code = report(self.session, "send", "2026-10-06")
        self.assertEqual(code, 1)
        self.assertNotIn("SECRET", str(result))

    def test_workflow_is_separate_dispatchable_and_has_seoul_schedule(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/news-report.yml"
        text = path.read_text(encoding="utf-8")
        self.assertIn("name: 뉴스 브리핑 Teams 정기 보고", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("default: preview", text)
        self.assertIn("'0-55/5 23 * * *'", text)
        self.assertIn("'0 0 * * *'", text)
        self.assertIn("scripts\\wind_news\\report_teams.ps1", text)
        self.assertNotIn("${{ inputs.issue_date }}", text.split("run:")[-1])
