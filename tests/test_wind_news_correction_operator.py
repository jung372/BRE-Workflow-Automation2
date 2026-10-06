import copy
import importlib.util
import unittest

if not all(importlib.util.find_spec(x) for x in ("duckdb", "polars")):
    raise unittest.SkipTest("wind-news isolated dependencies are not installed")

from scripts.wind_news.correct_duplicates import published_members


class CorrectionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.issue = {"issue": {"items": [{"event_id": "selected", "representative_article_id": "a"}]},
                      "candidates": [{"item": {"event_id": "selected"}, "approved": True, "held": False, "member_article_ids": ["a", "b"]},
                                     {"item": {"event_id": "beyond-cap"}, "approved": True, "held": False, "member_article_ids": ["c"]},
                                     {"item": {"event_id": "held"}, "approved": False, "held": True, "member_article_ids": ["d"]}]}

    def test_only_displayed_approved_events_are_selected(self):
        self.assertEqual(published_members(self.issue), ["a", "b"])

    def test_held_unapproved_or_missing_selected_member_refuses_correction(self):
        for field, value in (("held", True), ("approved", False), ("member_article_ids", ["b"])):
            issue = copy.deepcopy(self.issue)
            issue["candidates"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                published_members(issue)


if __name__ == "__main__":
    unittest.main()
