from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from logic.detector import KST
from logic.regular_report import post_report, read_json, report_lock, run_report, save_json
from presentation.teams_card import SITE_DISPLAY


class RegularReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "publish"
        self.runtime = self.root / "runtime"
        self.now = datetime(2026, 10, 19, 8, 30, tzinfo=KST)  # Monday
        self.sender = Mock(return_value={"status": "ACCEPTED", "http_status": 202})
        self.state = {site: {"hourly_snapshots": {"2026-10-16 08": [], "2026-10-19 08": []}}
                      for site in SITE_DISPLAY}
        self.state["notice"]["hourly_snapshots"]["2026-10-19 08"] = [
            {"id": "new", "title": "New notice", "date": "2026-10-19", "url": "https://example.org"}]
        self.status = {"checked_at": "2026-10-19 08:00:00", "metmasts": []}
        self.write_data()

    def write_data(self):
        save_json(self.data / "last_state.json", self.state)
        save_json(self.data / "data/status.json", self.status)

    def run_report(self, **kwargs):
        return run_report(self.data, self.runtime, "fixture-webhook", clock=lambda: self.now,
                          sender=self.sender, **kwargs)

    def test_monday_compares_friday_and_sends_only_once(self):
        outcome = self.run_report()
        self.assertEqual(outcome["status"], "ACCEPTED")
        self.assertEqual(outcome["comparison"], ["2026-10-16 08", "2026-10-19 08"])
        self.assertEqual(outcome["new_count"], 1)
        self.now += timedelta(minutes=5)
        self.assertEqual(self.run_report()["reason"], "DAILY_CLAIM_EXISTS")
        self.sender.assert_called_once()

    def test_definite_failure_retries_once_per_slot_until_0855(self):
        self.sender.return_value = {"status": "FAILED", "http_status": 429}
        for minute in range(30, 60, 5):
            self.now = self.now.replace(minute=minute)
            self.assertEqual(self.run_report()["status"], "FAILED")
            self.assertEqual(self.run_report()["status"], "SLOT_ALREADY_ATTEMPTED")
        self.now = self.now.replace(hour=9, minute=0)
        self.assertEqual(self.run_report()["status"], "DEADLINE_MISSED")
        self.assertEqual(self.sender.call_count, 6)

    def test_unknown_never_retried_even_manual_force(self):
        self.sender.return_value = {"status": "UNKNOWN"}
        self.run_report()
        self.now += timedelta(minutes=5)
        self.assertEqual(self.run_report(manual=True, force=True)["status"], "UNKNOWN")
        self.sender.assert_called_once()

    def test_process_crash_during_post_preserves_unknown_claim(self):
        self.sender.side_effect = RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.run_report()
        self.sender.reset_mock(side_effect=True)
        self.assertEqual(self.run_report()["status"], "UNKNOWN")
        self.sender.assert_not_called()

    def test_no_new_items_is_exception_and_remains_recheckable(self):
        self.state["notice"]["hourly_snapshots"]["2026-10-19 08"] = []
        self.write_data()
        self.assertEqual(self.run_report()["status"], "NO_NEW_ITEMS")
        self.sender.assert_not_called()
        self.now = self.now.replace(hour=9, minute=0)
        self.assertEqual(self.run_report()["status"], "NO_NEW_ITEMS")

    def test_empty_0830_can_send_new_data_at_0835(self):
        original = self.state["notice"]["hourly_snapshots"]["2026-10-19 08"]
        self.state["notice"]["hourly_snapshots"]["2026-10-19 08"] = []
        self.write_data()
        self.run_report()
        self.state["notice"]["hourly_snapshots"]["2026-10-19 08"] = original
        self.write_data()
        self.now += timedelta(minutes=5)
        self.assertEqual(self.run_report()["status"], "ACCEPTED")

    def test_missing_today_is_not_no_news(self):
        for site in self.state.values():
            del site["hourly_snapshots"]["2026-10-19 08"]
        self.write_data()
        self.assertEqual(self.run_report()["status"], "DATA_NOT_READY")
        self.sender.assert_not_called()

    def test_missing_baseline_does_not_report_everything_as_new(self):
        del self.state["notice"]["hourly_snapshots"]["2026-10-16 08"]
        self.write_data()
        outcome = self.run_report()
        self.assertEqual(outcome["status"], "DATA_NOT_READY")
        self.assertEqual(outcome["new_count"], 0)

    def test_one_failed_source_does_not_hide_healthy_new_notices(self):
        del self.state["result"]["hourly_snapshots"]["2026-10-19 08"]
        self.write_data()
        outcome = self.run_report()
        self.assertEqual(outcome["status"], "ACCEPTED")
        self.assertEqual(outcome["missing_sites"], ["result"])
        self.assertIn("비교 데이터 미확보", self.sender.call_args.args[1]["sections"][-1]["text"])

    def test_stale_status_does_not_send_false_metmast_alert(self):
        self.status = {"checked_at": "2026-10-18 20:00:00", "metmasts": [{"id": "m", "name": "m", "status": "Offline"}]}
        self.write_data()
        self.assertEqual(self.run_report()["status"], "DATA_NOT_READY")
        self.sender.assert_not_called()

    def test_existing_offline_metmast_alert_preserved(self):
        self.state["notice"]["hourly_snapshots"]["2026-10-19 08"] = []
        self.status["metmasts"] = [{"id": "m", "name": "m", "status": "Offline"}]
        self.write_data()
        self.assertEqual(self.run_report()["status"], "ACCEPTED")

    def test_preparation_does_not_send_early(self):
        self.now = self.now.replace(minute=5)
        self.assertEqual(self.run_report()["status"], "READY")
        self.sender.assert_not_called()

    def test_weekend_and_late_execution_never_send(self):
        for now in (self.now.replace(day=24), self.now.replace(hour=9), self.now.replace(hour=15)):
            self.now = now
            self.assertIn(self.run_report()["status"], {"OUTSIDE_WINDOW", "DEADLINE_MISSED"})
        self.sender.assert_not_called()

    def test_final_clock_check_blocks_crossing_deadline(self):
        times = iter([self.now.replace(minute=59, second=20), self.now.replace(hour=9, minute=0)])
        result = run_report(self.data, self.runtime, "fixture", clock=lambda: next(times), sender=self.sender)
        self.assertEqual(result["status"], "DEADLINE_MISSED")
        self.sender.assert_not_called()

    def test_preview_does_not_claim_or_send(self):
        self.assertEqual(self.run_report(preview=True)["status"], "PREVIEWED")
        self.assertFalse((self.runtime / "teams-report/2026-10-19.json").exists())
        self.sender.assert_not_called()

    def test_scraper_and_deployment_locks_prevent_torn_reads(self):
        self.runtime.mkdir()
        for name in ("run.lock", "deploy.lock"):
            path = self.runtime / name
            path.touch()
            self.assertEqual(self.run_report()["status"], "DATA_BUSY")
            path.unlink()
        self.sender.assert_not_called()

    def test_concurrent_report_uses_os_lock(self):
        with report_lock(self.runtime / "teams-report/report.lock") as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.run_report()["status"], "BUSY")
        self.assertEqual(self.run_report()["status"], "ACCEPTED")

    def test_corrupt_ledger_fails_closed(self):
        path = self.runtime / "teams-report/2026-10-19.json"
        path.parent.mkdir(parents=True)
        path.write_text("{")
        self.assertEqual(self.run_report()["status"], "LEDGER_INVALID")
        self.sender.assert_not_called()

    def test_corrupt_state_is_not_no_news(self):
        (self.data / "last_state.json").write_text("{")
        self.assertEqual(self.run_report()["status"], "DATA_NOT_READY")
        self.sender.assert_not_called()


class TeamsTransportTests(unittest.TestCase):
    @patch("logic.regular_report.requests.post")
    def test_response_classification_and_no_redirects(self, post):
        for code, status in ((200, "ACCEPTED"), (202, "ACCEPTED"), (302, "FAILED"),
                             (429, "FAILED"), (500, "UNKNOWN")):
            post.return_value.status_code = code
            self.assertEqual(post_report("fixture", {})["status"], status)
            self.assertFalse(post.call_args.kwargs["allow_redirects"])

    @patch("logic.regular_report.requests.post")
    def test_ambiguous_timeout_is_not_retried_and_secret_not_exposed(self, post):
        for error, status in ((requests.ConnectTimeout, "FAILED"), (requests.ReadTimeout, "UNKNOWN"),
                              (requests.ConnectionError, "UNKNOWN")):
            post.side_effect = error("sensitive-fixture-url")
            result = post_report("fixture", {})
            self.assertEqual(result["status"], status)
            self.assertNotIn("sensitive", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
