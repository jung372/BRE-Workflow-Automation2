"""An unresolved physical overlap must never silently enter capacity totals."""
import copy
import tempfile
import unittest

from wind_map.core import WindError, date_value
from wind_map.snapshot import build_snapshot, publication_gates
from wind_map.store import Store
from wind_map_helpers import NOW, seed


class OverlapGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        seed(self.store)
        self.project = {"id": "overlap-project", "name": "검증용 중복 후보",
                        "name_evidence_id": "source-siru", "wind_type": None,
                        "not_independent_capacity_for_aggregate": True}
        self.store.import_bundle({"schema_version": 1,
            "projects": [self.project],
            "permits": [{"id": "overlap-permit", "project_id": "overlap-project",
                         "authority": "검증용", "number": "synthetic-overlap"}],
            "events": [{"id": "overlap-observation", "permit_id": "overlap-permit",
                        "type": "registry_observation", "date": date_value("2026-05-13"),
                        "known_at": NOW, "review_state": "approved", "evidence_id": "source-siru",
                        "capacity_w": 20_000_000}]}, actor="test", reason="synthetic overlap")

    def test_untyped_candidate_is_excluded_but_classification_cannot_bypass_hold(self):
        self.assertFalse(publication_gates(self.store))
        build_snapshot(self.store, "2026-11-12", NOW, preview=True)
        onshore = {**copy.deepcopy(self.project), "wind_type": "onshore"}
        self.store.import_bundle({"schema_version": 1, "projects": [onshore]},
                                 actor="test", reason="synthetic classification")
        self.assertIn("Unresolved capacity overlap: overlap-project", publication_gates(self.store))
        for preview in (False, True):
            with self.subTest(preview=preview), self.assertRaisesRegex(WindError, "Unresolved capacity overlap"):
                build_snapshot(self.store, "2026-11-12", NOW, preview=preview)

    def test_resolved_overlap_flag_allows_existing_review_pipeline(self):
        resolved = {**copy.deepcopy(self.project), "wind_type": "onshore",
                    "not_independent_capacity_for_aggregate": False}
        self.store.import_bundle({"schema_version": 1, "projects": [resolved]},
                                 actor="test", reason="synthetic resolved overlap")
        self.assertFalse(publication_gates(self.store))
        self.assertIn("projects/overlap-project.json",
                      build_snapshot(self.store, "2026-11-12", NOW)["files"])

    def test_other_explicit_overlap_flags_also_block(self):
        for flag in ("potential_physical_overlap_example_unresolved",
                     "same_parcel_example_other_project_overlap_unresolved"):
            with self.subTest(flag=flag):
                project = {**copy.deepcopy(self.project), "wind_type": "onshore",
                           "not_independent_capacity_for_aggregate": False, flag: True}
                self.store.import_bundle({"schema_version": 1, "projects": [project]},
                                         actor="test", reason="synthetic unresolved overlap")
                self.assertIn("Unresolved capacity overlap: overlap-project", publication_gates(self.store))
                with self.assertRaisesRegex(WindError, "Unresolved capacity overlap"):
                    build_snapshot(self.store, "2026-11-12", NOW, preview=True)


if __name__ == "__main__":
    unittest.main()
