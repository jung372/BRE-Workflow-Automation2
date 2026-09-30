import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

if not all(importlib.util.find_spec(x) for x in ("duckdb", "polars")):
    raise unittest.SkipTest("wind-news isolated dependencies are not installed")

from news.store import Conflict, Store, canonical, utcnow


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "news.duckdb")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def verified(self):
        self.store.call(lambda db: db.execute("INSERT INTO issues VALUES ('wind-2026-09-30',1,'2026-09-30','WEB_VERIFIED','hash','hash','{}','[]','[]',NULL,?,?)", [utcnow(), utcnow()]).fetchall(), write=True)

    def test_idempotency_persists_and_rejects_changed_payload(self):
        first = self.store.submit("ingest", "one", {"articles": []})
        self.assertEqual(first["job_id"], self.store.submit("ingest", "one", {"articles": []})["job_id"])
        with self.assertRaises(Conflict):
            self.store.submit("ingest", "one", {"articles": [1]})
        self.store.close()
        self.store = Store(Path(self.temp.name) / "news.duckdb")
        self.assertEqual(first["job_id"], self.store.submit("ingest", "one", {"articles": []})["job_id"])

    def test_recovery_requeues_jobs_but_never_resends_uncertain_delivery(self):
        queued = self.store.submit("collect", "crash", {})
        self.assertEqual(self.store.next_job()["job_id"], queued["job_id"])
        self.verified()
        claimed = self.store.claim_delivery("wind-2026-09-30", 1, "test")
        self.assertTrue(claimed["claimed"])
        recovered = self.store.recover()
        self.assertIn(queued["job_id"], recovered["requeued_jobs"])
        again = self.store.claim_delivery("wind-2026-09-30", 1, "test")
        self.assertFalse(again["claimed"])
        self.assertEqual(again["status"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.store.delivery_result(claimed["delivery_id"], "DELIVERED")
        self.assertEqual(self.store.delivery_result(claimed["delivery_id"], "DELIVERED", {"message_id": "verified-123"})["status"], "DELIVERED")

    def test_only_web_verified_issue_can_claim_and_failures_are_bounded(self):
        with self.assertRaises(Conflict):
            self.store.claim_delivery("wind-missing", 1, "test")
        self.verified()
        for attempt in range(3):
            claimed = self.store.claim_delivery("wind-2026-09-30", 1, "test")
            self.assertTrue(claimed["claimed"])
            self.store.delivery_result(claimed["delivery_id"], "FAILED")
        self.assertFalse(self.store.claim_delivery("wind-2026-09-30", 1, "test")["claimed"])

    def test_accepted_is_not_delivered_and_cannot_reclaim(self):
        self.verified()
        claim = self.store.claim_delivery("wind-2026-09-30", 1, "test")
        result = self.store.delivery_result(claim["delivery_id"], "ACCEPTED")
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertFalse(self.store.claim_delivery("wind-2026-09-30", 1, "test")["claimed"])

    def test_consistent_backup_restores_jobs_and_publication(self):
        job = self.store.submit("collect", "backup", {})
        self.verified()
        destination = Path(self.temp.name) / "backups" / "snapshot.duckdb"
        self.store.backup(destination)
        restored = Store(destination)
        try:
            self.assertEqual(restored.job(job["job_id"])["status"], "QUEUED")
            self.assertEqual(restored.issue("wind-2026-09-30")["state"], "WEB_VERIFIED")
        finally:
            restored.close()

    def test_finished_ingest_job_does_not_retain_raw_text(self):
        job = self.store.submit("ingest", "text", {"articles": [{"text": "private source excerpt"}]})
        self.store.finish_job(job["job_id"], result={"batch_id": job["job_id"]})
        self.assertNotIn("private source excerpt", canonical(self.store.job(job["job_id"])))


if __name__ == "__main__":
    unittest.main()
