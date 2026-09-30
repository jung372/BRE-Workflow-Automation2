import unittest

from scripts.wind_news.oauth_device import parse_challenge


class DeviceChallengeTests(unittest.TestCase):
    def test_only_expected_openai_device_flow(self):
        self.assertEqual(parse_challenge("Visit https://auth.openai.com/codex/device\n\x1b[1mABCD-12345\x1b[0m"),
                         {"url": "https://auth.openai.com/codex/device", "code": "ABCD-12345"})
        self.assertIsNone(parse_challenge("https://evil.example/codex/device ABCD-12345"))
        self.assertIsNone(parse_challenge("https://auth.openai.com/codex/device no code"))


if __name__ == "__main__":
    unittest.main()
