"""Verify scoped schedule upgrades preserve the live endpoint and credentials."""
import copy
import unittest

from scripts.wind_news.build_workflows import build
from scripts.wind_news.configure_briefing import updated


class BriefingOperatorTests(unittest.TestCase):
    def test_upgrade_keeps_live_url_credential_ids_connections_and_other_nodes(self):
        template = next(f for f in build() if f["name"] == "BRE-WIND-04-Deliver")
        previous = copy.deepcopy(template)
        previous.update(id="fixtureworkflow1", active=True)
        previous["settings"]["errorWorkflow"] = "fixture-error"
        submit = next(n for n in previous["nodes"] if n["name"] == "Submit")
        submit["credentials"]["httpHeaderAuth"]["id"] = "fixture-credential"
        submit["parameters"]["url"] = "http://fixture-news-service:8090/v1/deliveries/send"
        submit["parameters"]["jsonBody"] = "previous-body"
        # Simulate the former synchronous Deliver, with no job polling/status nodes.
        previous["nodes"] = [n for n in previous["nodes"] if n["name"] in {
            "Schedule", "Manual test", "Configure", "Submit", "Check delivery", "Setup instructions"}]
        previous["connections"] = {name: copy.deepcopy(previous["connections"][name]) for name in ("Schedule", "Manual test")}
        previous["connections"].update(Configure={"main": [[{"node": "Submit", "type": "main", "index": 0}]]},
            Submit={"main": [[{"node": "Check delivery", "type": "main", "index": 0}]]})
        previous["connections"]["Manual test"]["main"][0][0]["marker"] = "operator-setting"
        result = updated(previous, template)
        self.assertEqual(result["id"], previous["id"])
        self.assertEqual(result["settings"]["errorWorkflow"], "fixture-error")
        self.assertEqual(result["connections"]["Manual test"], previous["connections"]["Manual test"])
        changed = next(n for n in result["nodes"] if n["name"] == "Submit")
        self.assertEqual(changed["parameters"]["url"], "http://fixture-news-service:8090/v1/briefings/check")
        for n in result["nodes"]:
            if n["type"].endswith("httpRequest"):
                self.assertEqual(n["credentials"], submit["credentials"])
        self.assertEqual(changed["credentials"], submit["credentials"])
        self.assertIn("issue_date", changed["parameters"]["jsonBody"])
        self.assertEqual(submit["parameters"]["jsonBody"], "previous-body")
        self.assertIn("Read daily status", {n["name"] for n in result["nodes"]})
        self.assertIn("Poll job", {n["name"] for n in result["nodes"]})

    def test_refuses_wrong_workflow_or_missing_node_before_writes(self):
        template = next(f for f in build() if f["name"] == "BRE-WIND-03-Publish")
        previous = copy.deepcopy(template)
        previous["name"] = "Unrelated workflow"
        with self.assertRaises(ValueError):
            updated(previous, template)
        previous["name"] = template["name"]
        previous["nodes"] = [n for n in previous["nodes"] if n["name"] != "Schedule"]
        with self.assertRaises(ValueError):
            updated(previous, template)
