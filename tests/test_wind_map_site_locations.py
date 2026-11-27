"""Site-address chronology and source-bound coordinates, using synthetic data."""
import json
import tempfile
import unittest

from wind_map.core import date_value
from wind_map.geography import select_site_location
from wind_map.snapshot import build_snapshot
from wind_map.store import Store
from wind_map_helpers import NOW, seed


def location(ident, source, date, address, **kw):
    return {"id": ident, "project_id": "siru", "source_type": source,
            "date": date_value(date), "addresses": [address], "scope": "whole_site",
            "known_at": NOW, "review_state": "approved", "evidence_id": "source-siru", **kw}


class LocationTests(unittest.TestCase):
    def test_evaluation_and_later_permits_follow_document_time(self):
        rows = [location("permit", "electricity_business_permit", "2024-01-01", "옛 필지"),
                location("eia", "environmental_assessment", "2025-01-01", "평가 필지")]
        self.assertEqual(select_site_location(rows, "2026-11-12")[0]["id"], "eia")
        for kind in ("electricity_business_permit", "development_permit", "construction_plan_approval"):
            later = location("later", kind, "2026-01-01", "후행 필지")
            self.assertEqual(select_site_location(rows + [later], "2026-11-12")[0]["id"], "later")

    def test_overlapping_dates_partial_and_unknown_do_not_guess(self):
        base = location("a", "environmental_assessment", "2025-06", "필지 A")
        for other in (location("b", "development_permit", "2025-06-15", "필지 B"),
                      location("b", "development_permit", "2026-01-01", "필지 B", scope="partial"),
                      location("b", "construction_plan_approval", None, "필지 B")):
            with self.subTest(other=other):
                value, warning = select_site_location([base, other], "2026-11-12")
                self.assertIsNone(value)
                self.assertTrue(warning)

    def test_snapshot_shows_address_and_does_not_reuse_old_pin(self):
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            seed(store)
            site = location("eia", "environmental_assessment", "2026-06-01", "평가 필지")
            pin = {"id": "pin", "project_id": "siru", "date": date_value("2026-06-01"),
                   "known_at": NOW, "review_state": "approved", "evidence_id": "source-siru",
                   "kind": "parcel", "region_check": "inside", "x": 129, "y": 36}
            store.import_bundle({"schema_version": 1, "site_locations": [site], "coordinates": [pin]},
                                actor="test", reason="synthetic")
            detail = json.loads(build_snapshot(store, "2026-11-12", NOW)["files"]["projects/siru.json"])
            self.assertEqual(detail["site_location"]["addresses"], ["평가 필지"])
            self.assertIsNone(detail["coordinate"])
            self.assertIn("source-siru", [s["id"] for s in detail["sources"]])
            pin2 = {**pin, "id": "pin-linked", "site_location_id": "eia"}
            store.import_bundle({"schema_version": 1, "coordinates": [pin2]}, actor="test", reason="synthetic")
            detail = json.loads(build_snapshot(store, "2026-11-12", NOW)["files"]["projects/siru.json"])
            self.assertEqual(detail["coordinate"]["longitude"], 129)


if __name__ == "__main__":
    unittest.main()
