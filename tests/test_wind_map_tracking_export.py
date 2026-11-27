import json
from pathlib import Path
import tempfile
import unittest

from wind_map.core import WindError
from wind_map.store import Store
from wind_map.tracking_export import export_tracking, make_tracking


class TrackingExportTests(unittest.TestCase):
    def test_unconfirmed_observation_is_visible_without_guessing_or_offshore(self):
        with tempfile.TemporaryDirectory() as root, Store(root) as store:
            doc = store.register_document(b"synthetic", {"source_key": "fixture", "agency": "test",
                "title": "테스트", "url": "https://example.com/test", "checked_at": "2026-01-01T00:00:00Z",
                "visibility": "public", "format": "html"})
            store.import_bundle({"schema_version": 1,
                "projects": [{"id": "land", "name": "육상 사업", "wind_type": "onshore", "name_evidence_id": "ev"},
                    {"id": "pending", "name": "후보 <검토>", "wind_type": None,
                     "tracking_status": "pending_source_identity_review", "possible_duplicate_not_resolved": True,
                     "same_observation_links": [{"project_id": "land", "permit_id": "permit"}],
                     "candidate_project_matches": [{"project_id": "land", "basis_note": "주소 일치 <검토>"}],
                     "not_independent_capacity_for_aggregate": True, "name_evidence_id": "pending-ev"},
                    {"id": "sea", "name": "해상", "wind_type": "offshore"}],
                "evidence": [{"id": "ev", "document_id": doc, "quote": "실제 승인일 미확인", "publication_approved": True},
                    {"id": "event-ev", "document_id": doc, "quote": "사건에만 연결된 근거", "publication_approved": False},
                    {"id": "candidate-ev", "document_id": doc, "project_id_candidate": "pending",
                     "quote": "사업 연결은 후보", "limits": "신청 여부 <미확인>", "publication_approved": False},
                    {"id": "corroboration", "document_id": doc, "confirms_existing_event_id": "event",
                     "quote": "기존 이력에 대한 추가 출처", "publication_approved": False},
                    {"id": "pending-ev", "document_id": doc, "quote": "신청 예정 <script>값</script>", "publication_approved": False}],
                "permits": [{"id": "permit", "project_id": "land", "authority": "검증용", "number": "검증용"}],
                "events": [{"id": "event", "permit_id": "permit", "evidence_id": "event-ev", "type": "registry_observation",
                    "date": {"start": "2020-01-01", "end": "2020-01-01", "precision": "day", "estimated": False},
                    "known_at": "2026-01-01T00:00:00Z", "review_state": "approved", "capacity_w": 60_000_000}]}, actor="test", reason="fixture")
            before = store.db.total_changes
            data = make_tracking(store)
            self.assertEqual(store.db.total_changes, before)
            self.assertEqual(data["counts"], {"onshore_observations": 1, "scope_unconfirmed_observations": 1})
            pending = next(x for x in data["records"] if x["project"]["id"] == "pending")
            self.assertEqual(pending["scope"], "unknown")
            pending_sources = {x["evidence"]["id"]: x for x in pending["sources"]}
            self.assertEqual(pending_sources["pending-ev"]["evidence"]["quote"], "신청 예정 <script>값</script>")
            self.assertTrue(pending_sources["candidate-ev"]["candidate_link"])
            self.assertFalse(pending_sources["pending-ev"]["candidate_link"])
            land = next(x for x in data["records"] if x["project"]["id"] == "land")
            self.assertEqual(len(land["events"]), 1)
            self.assertEqual({x["evidence"]["id"] for x in land["sources"]}, {"ev", "event-ev", "corroboration"})
            target = Path(root) / "operator" / "view"
            result = export_tracking(store, target)
            self.assertEqual(result["counts"], data["counts"])
            page = (target / "tracking.html").read_text(encoding="utf-8")
            self.assertIn("후보 &lt;검토&gt;", page)
            self.assertIn("신청 예정 &lt;script&gt;값&lt;/script&gt;", page)
            self.assertIn("허가 번호 관측: 검증용", page)
            self.assertIn("용량 관측 60.0 MW", page)
            self.assertIn("사건에만 연결된 근거", page)
            self.assertIn("사업 연결은 후보", page)
            self.assertIn("신청 여부 &lt;미확인&gt;", page)
            self.assertIn("기존 이력에 대한 추가 출처", page)
            self.assertIn("연결 후보", page)
            self.assertIn('href="#project-land"', page)
            self.assertIn("허가증 전체의 동일성은 별도 확인", page)
            self.assertIn("관련 사업 후보", page)
            self.assertIn("주소 일치 &lt;검토&gt;", page)
            self.assertIn("동일 허가 여부 미확정", page)
            self.assertNotIn("<script>값</script>", page)
            self.assertEqual(len(json.loads((target / "tracking.json").read_text(encoding="utf-8"))["records"]), 2)
            with self.assertRaises(WindError):
                export_tracking(store, target)
            with self.assertRaises(WindError):
                export_tracking(store, Path(root) / "outside-operator")


if __name__ == "__main__":
    unittest.main()
