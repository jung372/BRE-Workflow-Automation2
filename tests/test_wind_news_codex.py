import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if not importlib.util.find_spec("jsonschema"):
    raise unittest.SkipTest("OAuth adapter tests require news/requirements.txt")

from news.codex_summary import CodexSummary


class FakeCodex:
    def __init__(self):
        self.calls = []
        self.auth = "Logged in using ChatGPT"
        self.summary = {"summary": "대한풍력은 전남 바람사업 MOU를 체결했다.",
            "evidence_quotes": ["대한풍력은 전남 바람사업 MOU를 체결했다."],
            "companies": ["대한풍력"], "project_name": "바람사업", "region": "전남"}
        self.verdict = {"supported": True, "contradictions": []}

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[1:3] == ["login", "status"]:
            return SimpleNamespace(returncode=0, stdout=self.auth, stderr="")
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(json.dumps(self.verdict if output.name == "review.json" else self.summary), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "auth").mkdir()
        self.env = patch.dict(os.environ, {"WIND_NEWS_CODEX_HOME": str(self.root / "auth"),
            "WIND_NEWS_RUNTIME_DIR": str(self.root / "runtime"), "OPENAI_API_KEY": "fake-must-not-inherit",
            "WIND_NEWS_API_TOKEN": "fake-must-not-inherit", "WIND_NEWS_TEAMS_WEBHOOK_URL": "fake-private"})
        self.env.start()
        self.fake = FakeCodex()
        self.config = {"enabled": True, "model": "gpt-6.1-sol", "review_model": "gpt-6.1-sol",
            "reasoning_effort": "low", "review_reasoning_effort": "medium", "max_calls_per_day": 4}
        self.adapter = CodexSummary(self.config, runner=self.fake)
        self.article = {"title": "풍력 MOU", "description": self.fake.summary["summary"], "evidence_scope": "description"}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_oauth_two_roles_no_tools_or_api_keys_and_persistent_cache(self):
        result = self.adapter(self.article)
        self.assertTrue(result["valid"])
        self.assertFalse(result["review_required"])
        self.assertEqual(result["metadata"]["companies"], ["대한풍력"])
        calls = self.fake.calls
        self.assertEqual(len(calls), 3)
        for args, kwargs in calls[1:]:
            self.assertFalse(kwargs["shell"])
            self.assertIn("--ephemeral", args)
            self.assertIn("--ignore-user-config", args)
            self.assertIn("features.shell_tool=false", args)
            self.assertIn('forced_login_method="chatgpt"', args)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("WIND_NEWS_API_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str((self.root / "auth").resolve()))
        self.assertIn('model_reasoning_effort="low"', calls[1][0])
        self.assertIn('model_reasoning_effort="medium"', calls[2][0])
        fresh = CodexSummary(self.config, runner=self.fake)
        self.assertEqual(fresh(self.article), result)
        self.assertEqual(len(calls), 3)
        self.assertEqual(list((self.root / "runtime/codex-jobs").iterdir()), [])

    def test_api_key_login_is_refused_without_summary_call(self):
        self.fake.auth = "Logged in using API key"
        result = self.adapter(self.article)
        self.assertFalse(result["valid"])
        self.assertEqual(len(self.fake.calls), 1)

    def test_unsupported_company_or_fabricated_summary_is_held(self):
        self.fake.summary["companies"] = ["허구기업"]
        result = self.adapter(self.article)
        self.assertEqual(result["fallback_reason"], "CODEX_EVIDENCE_INVALID")
        self.assertEqual(result["summary"], "")
        self.assertEqual(len(self.fake.calls), 2)

    def test_independent_review_rejection_is_not_automatic_approval(self):
        self.fake.verdict = {"supported": False, "contradictions": ["stage uncertain"]}
        self.assertEqual(self.adapter(self.article)["fallback_reason"], "CODEX_REVIEW_REJECTED")

    def test_persistent_daily_limit_survives_restart(self):
        self.config["max_calls_per_day"] = 2
        self.assertTrue(self.adapter(self.article)["valid"])
        other = {**self.article, "description": self.article["description"] + " 추가 기사."}
        result = CodexSummary(self.config, runner=self.fake)(other)
        self.assertEqual(result["fallback_reason"], "CODEX_DAILY_CALL_LIMIT")
        self.assertEqual(len(self.fake.calls), 3)

    def test_cli_failure_does_not_expose_auth_diagnostics(self):
        def broken(*args, **kwargs):
            raise subprocess.TimeoutExpired("secret-in-diagnostic", 1)
        result = CodexSummary(self.config, runner=broken)(self.article)
        self.assertEqual(result["fallback_reason"], "CODEX_UNAVAILABLE")
        self.assertNotIn("secret-in-diagnostic", json.dumps(result))

    def test_disabled_never_invokes_cli(self):
        self.config["enabled"] = False
        self.assertFalse(self.adapter(self.article)["valid"])
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()
