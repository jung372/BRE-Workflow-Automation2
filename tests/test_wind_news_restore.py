import tempfile
import json
from pathlib import Path
import unittest

try:
    import duckdb  # noqa: F401
    import polars  # noqa: F401
except ImportError:
    raise unittest.SkipTest("News database tests require news/requirements.txt")

from news.store import Store
from scripts.wind_news.restore_backup import restore


class RestoreTest(unittest.TestCase):
    def test_restored_published_history_rebuilds_identical_archive(self):
        from news.pipeline import Pipeline
        from news.publisher import build_snapshot
        from news.store import canonical
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Store(root / "live.duckdb")
            collection = {"sources": [{"source_id": "fixture", "name": "Fixture",
                "hosts": ["example.kr"], "enabled": True, "required": True,
                "domestic": True, "language": "ko", "rights_reviewed": True}]}
            pipeline = Pipeline(db, collection, {"automatic_publication": False})
            fixture = json.loads((Path(__file__).parent / "fixtures/wind_news/articles.json").read_text(encoding="utf-8"))
            try:
                pipeline.ingest(fixture, "restore-fixture")
                record = pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["restore-fixture"]})
                record = pipeline.review(record["issue_id"], {"revision": record["revision"],
                    "approval_hash": record["content_hash"], "action": "approve", "reason": "fixture review"})
                public = record["payload"]
                public["published_at"] = "2026-09-30T08:00:00+09:00"
                db.call(lambda conn: conn.execute("UPDATE issues SET state='WEB_VERIFIED',payload=?", [canonical(public)]).fetchall(), write=True)
                expected = build_snapshot([{**public, "state": "WEB_VERIFIED", "approved": True}], root / "before")
                saved = db.backup(root / "snapshot.duckdb")
            finally:
                db.close()
            restore(root / "snapshot.duckdb", saved["sha256"], root / "restored.duckdb")
            recovered = Store(root / "restored.duckdb")
            try:
                history = recovered.list_issues(published=True)
                actual = build_snapshot([{**row["payload"], "state": row["state"], "approved": True} for row in history], root / "after")
                self.assertEqual(expected["files"], actual["files"])
                self.assertEqual(actual["manifest"]["months"][0]["count"], 2)
            finally:
                recovered.close()

    def test_consistent_backup_restores_to_new_file_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Store(root / "live.duckdb")
            try:
                db.submit("ingest", "fixture-1", {"articles": []})
                saved = db.backup(root / "backup.duckdb")
            finally:
                db.close()
            restored = restore(root / "backup.duckdb", saved["sha256"], root / "restored.duckdb")
            self.assertEqual(restored["counts"]["jobs"], 1)
            self.assertEqual(restored["sha256"], saved["sha256"])
            before = (root / "restored.duckdb").read_bytes()
            with self.assertRaisesRegex(ValueError, "destination_must_be_new"):
                restore(root / "backup.duckdb", saved["sha256"], root / "restored.duckdb")
            self.assertEqual((root / "restored.duckdb").read_bytes(), before)
            with self.assertRaisesRegex(ValueError, "hash_mismatch"):
                restore(root / "backup.duckdb", "0" * 64, root / "bad.duckdb")
            self.assertFalse((root / "bad.duckdb").exists())


if __name__ == "__main__":
    unittest.main()
