from pathlib import Path
import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

from scripts.wind_news.configure_teams import main, update_settings
from news.delivery import _webhook_allowed


class SharedTeamsConfigTests(unittest.TestCase):
    def test_official_receiver_hosts_match_configuration_and_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.env"
            path.write_text("WIND_NEWS_RELEASE=fixture\n", encoding="utf-8")
            for url in ("https://fixture.logic.azure.com/test", "https://fixture.api.powerplatform.com/test",
                        "https://fixture.webhook.office.com/webhookb2/test", "https://outlook.office.com/webhook/test"):
                self.assertTrue(_webhook_allowed(url))
                self.assertEqual(update_settings(path, url)[0], url)
            for url in ("https://evil-webhook.office.com/test", "https://fixture.webhook.office.com.evil.example/test",
                        "https://outlook.office.com/mail/test", "https://webhook.office.com/test"):
                self.assertFalse(_webhook_allowed(url))
                with self.assertRaises(ValueError):
                    update_settings(path, url)

    def test_diagnostic_reports_fixed_stage_without_exception_secret(self):
        output = io.StringIO()
        with patch("scripts.wind_news.configure_teams.Path.read_text", side_effect=RuntimeError("private-secret")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(), 1)
        self.assertEqual(output.getvalue(), "SHARED_TEAMS_CONFIG_FAILED stage=SERVICE_VERSION code=UNEXPECTED_ERROR\n")

    def test_updates_only_teams_keys_preserving_unrelated_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.env"
            path.write_text("# private settings\nWIND_NEWS_API_TOKEN=fixture-token\n"
                            "WIND_NEWS_RELEASE=abc\nWIND_NEWS_TEAMS_WEBHOOK_URL=old\n"
                            "WIND_NEWS_TEAMS_WEBHOOK_URL=duplicate\n", encoding="utf-8")
            webhook = "https://fixture.logic.azure.com/workflows/test?sig=fixture$literal"
            _, changed = update_settings(path, webhook)
            self.assertTrue(changed)
            value = path.read_text(encoding="utf-8")
            self.assertIn("# private settings\nWIND_NEWS_API_TOKEN=fixture-token\nWIND_NEWS_RELEASE=abc\n", value)
            self.assertEqual(value.count("WIND_NEWS_TEAMS_WEBHOOK_URL="), 1)
            self.assertIn(f"WIND_NEWS_TEAMS_WEBHOOK_URL='{webhook}'", value)
            self.assertIn("WIND_NEWS_TEAMS_CARD_FORMAT=messagecard", value)
            self.assertFalse(update_settings(path, webhook)[1])

    def test_invalid_secret_does_not_change_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.env"
            original = "WIND_NEWS_API_TOKEN=fixture-token\n"
            path.write_text(original, encoding="utf-8")
            for url in ("", "http://localhost/", "https://evil-logic.azure.com/",
                        "https://fixture.logic.azure.com/?sig=x\nINJECTED=x",
                        "https://fixture.logic.azure.com/?sig='", "https://user:pass@fixture.logic.azure.com/"):
                with self.assertRaises(ValueError):
                    update_settings(path, url)
                self.assertEqual(path.read_text(encoding="utf-8"), original)
