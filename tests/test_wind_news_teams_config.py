from pathlib import Path
import tempfile
import unittest

from scripts.wind_news.configure_teams import update_settings


class SharedTeamsConfigTests(unittest.TestCase):
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
