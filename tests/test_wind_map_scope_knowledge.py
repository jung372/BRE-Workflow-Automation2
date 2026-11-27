"""A later scope determination must not rewrite earlier knowledge snapshots."""
import copy
import json
import tempfile
import unittest

from wind_map.core import WindError, date_value
from wind_map.snapshot import build_snapshot
from wind_map.store import Store
from wind_map_helpers import NOW, seed

LATER = "2026-11-13T12:00:00+09:00"


class ScopeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        seed(self.store)
        self.project = {"id": "later-scope", "name": "검증용 유형 후행확인 사업",
                        "name_evidence_id": "source-siru", "wind_type": None}
        document = self.store.register_document(b"synthetic scope evidence", {
            "source_key": "synthetic-later-scope", "agency": "검증용 기관",
            "title": "검증용 유형 확인", "url": "https://example.org/synthetic-scope",
            "checked_at": LATER, "visibility": "public", "format": "html"})
        self.store.import_bundle({"schema_version": 1,
            "projects": [self.project],
            "permits": [{"id": "later-scope-permit", "project_id": "later-scope",
                         "authority": "검증용 기관", "number": "synthetic-2"}],
            "evidence": [{"id": "source-later-scope", "document_id": document,
                          "section": "synthetic", "quote": "검증용 육상유형 확인",
                          "publication_approved": True}],
            "events": [{"id": "later-scope-observation", "permit_id": "later-scope-permit",
                        "type": "registry_observation", "date": date_value("2026-05-13"),
                        "known_at": NOW, "review_state": "approved", "evidence_id": "source-siru",
                        "capacity_w": 20_000_000}],
            "allocation_sets": [{"id": "later-scope-allocation", "permit_id": "later-scope-permit",
                "date": date_value("2026-05-13"), "known_at": NOW, "review_state": "approved",
                "evidence_id": "source-siru", "capacity_event_id": "later-scope-observation",
                "parts": [{"region_id": "47130", "capacity_w": 20_000_000}]}]},
            actor="test", reason="synthetic pending type with previously known facts")

    def snapshot(self, known):
        return build_snapshot(self.store, "2026-09-30", known, preview=True)

    def national_capacity(self, snapshot):
        totals = json.loads(snapshot["files"]["regions.json"])["totals"]["all"]
        return next(row["capacity_w"] for row in totals if row["region_id"] == "KR")

    def approve_type(self, **changes):
        project = {**copy.deepcopy(self.project), "wind_type": "onshore",
                   "wind_type_known_at": LATER, "wind_type_evidence_id": "source-later-scope",
                   **changes}
        self.store.import_bundle({"schema_version": 1, "projects": [project]},
                                 actor="test", reason="synthetic later type confirmation")

    def test_later_type_preserves_prior_snapshot_and_is_inclusive_at_confirmation(self):
        before = self.snapshot(NOW)
        self.assertEqual(self.national_capacity(before), 60_000_000)
        self.approve_type()
        earlier = self.snapshot(NOW)
        self.assertEqual(json.loads(earlier["files"]["regions.json"])["totals"],
                         json.loads(before["files"]["regions.json"])["totals"])
        self.assertNotIn("projects/later-scope.json", earlier["files"])
        confirmed = self.snapshot(LATER)
        self.assertEqual(self.national_capacity(confirmed), 80_000_000)
        self.assertIn("projects/later-scope.json", confirmed["files"])
        detail = json.loads(confirmed["files"]["projects/later-scope.json"])
        self.assertIn("source-later-scope", {source["id"] for source in detail["sources"]})

    def test_invalid_scope_time_and_future_scope_evidence_fail_closed(self):
        self.approve_type(wind_type_known_at="invalid")
        with self.assertRaises(WindError):
            self.snapshot(NOW)
        self.approve_type(wind_type_known_at=NOW)
        with self.assertRaisesRegex(WindError, "Evidence was unavailable"):
            self.snapshot(NOW)

    def test_legacy_onshore_project_without_scope_timestamp_remains_supported(self):
        self.store.import_bundle({"schema_version": 1,
            "projects": [{**self.project, "wind_type": "onshore"}]},
            actor="test", reason="synthetic legacy project")
        self.assertEqual(self.national_capacity(self.snapshot(NOW)), 80_000_000)

    def test_later_region_preserves_old_dimensions_and_appears_at_confirmation(self):
        before = json.loads(self.snapshot(NOW)["files"]["regions.json"])["totals"]
        self.store.import_bundle({"schema_version": 1, "regions": [
            {"id": "later-province", "name": "검증용 도", "parent_id": "KR",
             "code_year": 2026, "known_at": LATER},
            {"id": "later-county", "name": "검증용 군", "parent_id": "later-province",
             "code_year": 2026, "known_at": LATER}]}, actor="test", reason="synthetic later region")
        self.assertEqual(json.loads(self.snapshot(NOW)["files"]["regions.json"])["totals"], before)
        confirmed = json.loads(self.snapshot(LATER)["files"]["regions.json"])["totals"]["all"]
        self.assertEqual({row["region_id"] for row in confirmed} -
                         {row["region_id"] for row in before["all"]},
                         {"later-province", "later-county"})
        with self.store.transaction():
            self.store.put("regions", {"id": "later-county", "name": "검증용 군",
                "parent_id": "later-province", "code_year": 2026, "known_at": "invalid"},
                actor="test", reason="synthetic invalid knowledge timestamp")
        with self.assertRaises(WindError):
            self.snapshot(NOW)


if __name__ == "__main__":
    unittest.main()
