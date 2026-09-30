"""Offline contract checks for importable n8n exports."""
import json
from pathlib import Path
import unittest

from scripts.wind_news.build_workflows import build

ROOT = Path(__file__).resolve().parents[1]


class WorkflowsTest(unittest.TestCase):
    def test_exports_match_generator_and_connections(self):
        for flow in build():
            with self.subTest(flow=flow["name"]):
                stored = json.loads((ROOT / "automation/n8n" / (flow["name"] + ".json")).read_text(encoding="utf-8"))
                self.assertEqual(stored, flow)
                self.assertFalse(flow["active"])
                self.assertEqual(flow["settings"]["timezone"], "Asia/Seoul")
                names = {n["name"] for n in flow["nodes"]}
                self.assertEqual(len(names), len(flow["nodes"]))
                for source, groups in flow["connections"].items():
                    self.assertIn(source, names)
                    for branch in groups["main"]:
                        for edge in branch:
                            self.assertIn(edge["node"], names)

    def test_all_requests_authenticate_without_inline_secret(self):
        for flow in build():
            for n in flow["nodes"]:
                if n["type"].endswith("httpRequest"):
                    p = n["parameters"]
                    self.assertEqual(p["genericAuthType"], "httpHeaderAuth")
                    self.assertNotIn("id", n["credentials"]["httpHeaderAuth"])
                    self.assertFalse(p["options"]["redirect"]["redirect"]["followRedirects"])
                    if n["name"] == "Submit" and "04-Deliver" not in flow["name"]:
                        self.assertEqual(p["headerParameters"]["parameters"][0]["name"], "Idempotency-Key")

    def test_publish_does_not_depend_on_teams(self):
        publish = next(w for w in build() if "03-Publish" in w["name"])
        self.assertNotIn("/v1/deliveries", json.dumps(publish))
        self.assertNotIn("executeWorkflow", json.dumps(publish))

    def test_jobs_have_bound_and_fail_explicitly(self):
        for flow in build():
            check = next((n for n in flow["nodes"] if n["name"] == "Check outcome"), None)
            if check:
                self.assertIn("deadline", check["parameters"]["jsCode"])
                self.assertIn("FAILED", check["parameters"]["jsCode"])


if __name__ == "__main__":
    unittest.main()
