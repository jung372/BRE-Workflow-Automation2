import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if not all(importlib.util.find_spec(x) for x in ("duckdb", "polars", "flask", "jsonschema")):
    raise unittest.SkipTest("wind-news isolated dependencies are not installed")

from news.service import create_app
from news.store import Conflict, Store

TOKEN = "fixture-token-do-not-use-in-production-123"
CONFIG = {"collection": {"enabled": False, "sources": [{"source_id": "fixture", "name": "시험매체", "hosts": ["example.kr"], "enabled": True, "required": True, "domestic": True, "language": "ko", "rights_reviewed": True}]}, "policy": {"max_items": 15, "automatic_publication": False, "publisher": {"enabled": True, "pages_base_url": "https://example.kr/"}}}


class FakePublisher:
    def __init__(self):
        self.state, self.calls = "WEB_VERIFIED", []

    def publish(self, issues):
        self.calls.append(copy.deepcopy(issues))
        return {"state": self.state, "commit_sha": "abc123", "snapshot_id": "fixture"}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "news.duckdb")
        self.publisher = FakePublisher()
        self.config = copy.deepcopy(CONFIG)
        self.app = create_app(self.config, store=self.store, runtime=self.temp.name, token=TOKEN, start_worker=False, publisher=self.publisher, summarizer=lambda a, c: {"summary": a["title"], "valid": True, "review_required": False})
        self.client = self.app.test_client()
        self.headers = {"Authorization": "Bearer " + TOKEN}
        self.runner = self.app.extensions["wind_news_runner"]
        self.pipeline = self.app.extensions["wind_news_pipeline"]
        self.fixture = json.loads((Path(__file__).parent / "fixtures" / "wind_news" / "articles.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.runner.stop()
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, path, payload, key):
        response = self.client.post(path, json=payload, headers={**self.headers, "Idempotency-Key": key})
        self.assertEqual(response.status_code, 202, response.json)
        return response.json["job_id"]

    def ready(self):
        self.pipeline.ingest(self.fixture, "fixture-batch")
        record = self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["fixture-batch"]})
        return self.pipeline.review(record["issue_id"], {"revision": 1, "approval_hash": record["content_hash"], "action": "approve", "reason": "fixture evidence checked"})

    def publish(self, record):
        return self.runner.publish({"issue_id": record["issue_id"], "revision": record["revision"], "approval_hash": record["approval_hash"]})

    def test_all_data_routes_require_auth_and_ui_contains_no_token(self):
        for route in ("/health/ready", "/v1/config/collection", "/v1/jobs/missing", "/v1/issues", "/v1/deliveries/pending"):
            self.assertEqual(self.client.get(route).status_code, 401)
        self.assertNotIn(TOKEN, self.client.get("/review").get_data(as_text=True))
        self.assertEqual(self.client.get("/health/ready", headers=self.headers).json["status"], "ready")

    def test_ingest_persisted_before202_job_idempotency_and_redacted_poll(self):
        job_id = self.enqueue("/v1/ingest", self.fixture, "ingest")
        self.assertEqual(self.store.job(job_id)["status"], "QUEUED")
        self.runner.run_once()
        response = self.client.get("/v1/jobs/" + job_id, headers=self.headers)
        self.assertEqual(response.json["status"], "SUCCEEDED")
        self.assertEqual(response.json["result"]["ingested"], 2)
        self.assertNotIn("payload", response.json)
        self.assertEqual(self.enqueue("/v1/ingest", self.fixture, "ingest"), job_id)
        self.assertEqual(self.client.post("/v1/ingest", json={}, headers={**self.headers, "Idempotency-Key": "ingest"}).status_code, 409)

    def test_publish_persists_web_without_teams_and_idempotent_repeat(self):
        record = self.ready()
        self.publish(record)
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": ""}):
            skipped = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
        self.assertEqual(skipped.json["status"], "SKIPPED")
        self.assertEqual(len(self.store.pending_deliveries()), 1)
        self.publish(record)
        self.assertEqual(len(self.publisher.calls), 1)
        self.assertEqual(self.store.issue(record["issue_id"])["state"], "WEB_VERIFIED")

    def test_timeout_unknown_never_repeated(self):
        self.publish(self.ready())
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "UNKNOWN", "error_code": "TIMEOUT"}) as send:
            first = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
            second = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
        self.assertEqual(first.json["status"], "UNKNOWN")
        self.assertEqual(second.json["status"], "UNKNOWN")
        self.assertEqual(send.call_count, 1)

    def test_preview_and_status_do_not_send_or_expose_webhook(self):
        self.publish(self.ready())
        self.runner.clock = lambda: datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/?sig=PRIVATE"}), patch("news.delivery.send_card") as send:
            status = self.client.get("/v1/deliveries/status", headers=self.headers)
            self.assertTrue(status.json["teams_configured"])
            self.assertEqual(status.json["issue_date"], "2026-09-30")
            self.assertNotIn("PRIVATE", status.get_data(as_text=True))
            preview = self.client.post("/v1/deliveries/preview", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.assertEqual(preview.status_code, 200)
            self.assertIn("Action.OpenUrl", str(preview.json))
            send.assert_not_called()
        self.assertEqual(self.client.get("/v1/deliveries/status").status_code, 401)
        self.assertEqual(self.client.post("/v1/deliveries/preview", json={}).status_code, 401)

    def test_shared_receiver_format_used_for_preview_and_send_and_historical_status(self):
        self.publish(self.ready())
        self.runner.clock = lambda: datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        settings = {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example",
                    "WIND_NEWS_TEAMS_CARD_FORMAT": "messagecard"}
        with patch.dict(os.environ, settings), patch("news.delivery.send_card", return_value={"status": "ACCEPTED"}) as send:
            preview = self.client.post("/v1/deliveries/preview", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.assertEqual(preview.json["@type"], "MessageCard")
            self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.assertEqual(send.call_args.kwargs["card_format"], "messagecard")
            status = self.client.get("/v1/deliveries/status?issue_date=2026-09-30", headers=self.headers)
            self.assertEqual(status.json["delivery_status"], "ACCEPTED")
            self.assertEqual(status.json["card_format"], "messagecard")
            self.assertEqual(status.json["issue_date"], "2026-09-30")
            bad = self.client.get("/v1/deliveries/status?issue_date=2026-02-30", headers=self.headers)
            self.assertEqual(bad.status_code, 400)

    def test_scheduled_window_and_date_guard_before_contacting_teams(self):
        self.publish(self.ready())
        payload = {"issue_date": "2026-09-30", "scheduled": True}
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "ACCEPTED"}) as send:
            for hour, minute in [(15, 0), (22, 59), (23, 0), (23, 29)]:
                self.runner.clock = lambda h=hour, m=minute: datetime(2026, 9, 29, h, m, tzinfo=timezone.utc)
                response = self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
                self.assertEqual(response.json["error_code"], "OUTSIDE_DELIVERY_WINDOW")
            self.runner.clock = lambda: datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)
            wrong = self.client.post("/v1/deliveries/send", json={**payload, "issue_date": "2026-09-29"}, headers=self.headers)
            self.assertEqual(wrong.status_code, 400)
            send.assert_not_called()
            first = self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
            self.assertEqual(first.json["status"], "ACCEPTED")
            self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
            self.assertEqual(send.call_count, 1)

    def assert_delayed_scheduled_send(self, hour, minute, outcome="ACCEPTED"):
        self.publish(self.ready())
        self.runner.clock = lambda: datetime(2026, 9, 30, hour, minute, tzinfo=timezone.utc)
        payload = {"issue_date": "2026-09-30", "scheduled": True}
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": outcome}) as send:
            first = self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
            self.assertEqual(first.json["status"], outcome)
            self.assertEqual(send.call_args.args[0]["issue_date"], "2026-09-30")
            self.runner.clock = lambda: datetime(2026, 9, 30, 14, 59, tzinfo=timezone.utc)
            repeat = self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
            self.assertEqual(repeat.json["status"], outcome)
            self.assertFalse(repeat.json["claimed"])
            self.assertEqual(send.call_count, 1)

    def test_scheduled_send_after_0900_is_allowed(self):
        self.assert_delayed_scheduled_send(0, 1)

    def test_scheduled_send_at_1550_is_allowed(self):
        self.assert_delayed_scheduled_send(6, 50)

    def test_scheduled_send_at_2359_is_allowed(self):
        self.assert_delayed_scheduled_send(14, 59)

    def test_delayed_scheduled_unknown_is_not_retried(self):
        self.assert_delayed_scheduled_send(6, 50, "UNKNOWN")

    def test_delayed_scheduled_date_and_revision_guards(self):
        self.publish(self.ready())
        self.runner.clock = lambda: datetime(2026, 9, 30, 6, 50, tzinfo=timezone.utc)
        payload = {"issue_date": "2026-09-30", "scheduled": True}
        with patch("news.delivery.send_card") as send:
            for invalid in ({"issue_date": "2026-09-29"}, {"issue_date": "2026-10-01"},
                            {"revision": 1}, {"message_type": "correction"}):
                with self.subTest(invalid=invalid):
                    response = self.client.post("/v1/deliveries/send", json={**payload, **invalid}, headers=self.headers)
                    self.assertEqual(response.status_code, 400)
            self.runner.clock = lambda: datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
            yesterday = self.client.post("/v1/deliveries/send", json=payload, headers=self.headers)
            self.assertEqual(yesterday.status_code, 400)
            send.assert_not_called()

    def test_deadline_for_missing_issue_and_no_yesterday_fallback(self):
        self.publish(self.ready())
        with patch("news.delivery.send_card") as send:
            for hour, minute, expected in [(23, 59, "WEB_VERIFICATION_REQUIRED"),
                                          (0, 0, "WEB_VERIFICATION_REQUIRED"),
                                          (0, 1, "WEB_VERIFICATION_REQUIRED"),
                                          (6, 50, "WEB_VERIFICATION_REQUIRED"),
                                          (14, 29, "WEB_VERIFICATION_REQUIRED"),
                                          (14, 30, "PUBLICATION_NOT_READY_BY_DEADLINE"),
                                          (14, 59, "PUBLICATION_NOT_READY_BY_DEADLINE")]:
                day, month = (30, 9) if hour == 23 else (1, 10)
                self.runner.clock = lambda h=hour, m=minute, d=day, mo=month: datetime(2026, mo, d, h, m, tzinfo=timezone.utc)
                with self.subTest(hour=hour, minute=minute):
                    response = self.client.post("/v1/deliveries/send", json={"scheduled": True}, headers=self.headers)
                    self.assertEqual(response.json["status"], "SKIPPED")
                    self.assertEqual(response.json["error_code"], expected)
            send.assert_not_called()

    def test_daily_does_not_resend_after_revision_changes(self):
        record = self.ready()
        self.publish(record)
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "ACCEPTED"}) as send:
            self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.store.call(lambda db: db.execute("INSERT INTO issues SELECT issue_id,2,issue_date,state,content_hash,approval_hash,payload,candidates,batch_ids,publish_result,created_at,updated_at FROM issues WHERE revision=1").fetchall(), write=True)
            repeat = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.assertFalse(repeat.json["claimed"])
            self.assertEqual(send.call_count, 1)

    def test_first_send_selects_verified_revision_beneath_newer_draft(self):
        self.publish(self.ready())
        self.store.call(lambda db: db.execute("INSERT INTO issues SELECT issue_id,2,issue_date,'DRAFT',content_hash,approval_hash,payload,candidates,batch_ids,publish_result,created_at,updated_at FROM issues WHERE revision=1").fetchall(), write=True)
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "ACCEPTED"}) as send:
            response = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers)
            self.assertEqual(response.json["status"], "ACCEPTED")
            self.assertEqual(send.call_args.args[0]["revision"], 1)
        record = self.client.get("/v1/issues/wind-2026-09-30?revision=1", headers=self.headers)
        self.assertEqual(record.json["revision"], 1)
        self.assertEqual(self.client.get("/v1/issues/wind-2026-09-30?revision=invalid", headers=self.headers).status_code, 400)

    def test_publishing_blocks_concurrent_edits(self):
        record = self.ready()
        def external(issues):
            with self.assertRaises(Conflict):
                self.pipeline.review(record["issue_id"], {"revision": 1, "approval_hash": record["content_hash"], "action": "edit", "item_ids": [record["candidates"][0]["item"]["event_id"]], "changes": {"summary": "changed"}, "reason": "race"})
            return {"state": "WEB_VERIFIED"}
        self.publisher.publish = external
        self.publish(record)
        self.assertEqual(self.store.issue(record["issue_id"])["state"], "WEB_VERIFIED")

    def test_bad_publisher_result_unlocks_byte_identical_retry(self):
        record = self.ready()
        self.publisher.state = "WRONG"
        with self.assertRaises(ValueError):
            self.publish(record)
        self.assertEqual(self.store.issue(record["issue_id"])["state"], "READY")
        self.publisher.state = "WEB_VERIFIED"
        self.publish(record)
        self.assertEqual(self.publisher.calls[0][-1]["published_at"], self.publisher.calls[1][-1]["published_at"])

    def test_failed_attempt_freezes_content_even_if_git_outcome_is_unknown(self):
        record = self.ready()
        self.publisher.publish = lambda issues: (_ for _ in ()).throw(ValueError("PUBLISH_INTERRUPTED"))
        with self.assertRaises(ValueError):
            self.publish(record)
        after = self.store.issue(record["issue_id"])
        self.assertEqual(after["state"], "READY")
        self.assertIsNotNone(after["payload"]["published_at"])
        with self.assertRaises(Conflict):
            self.pipeline.review(record["issue_id"], {"revision": 1, "approval_hash": record["content_hash"], "action": "edit", "item_ids": [record["candidates"][0]["item"]["event_id"]], "changes": {"summary": "changed"}, "reason": "unsafe mutation"})

    def test_schema_rejects_invalid_editorial_enum_before_network(self):
        record = self.ready()
        modified = self.pipeline.review(record["issue_id"], {"revision": 1, "approval_hash": record["content_hash"], "action": "edit", "item_ids": [record["candidates"][0]["item"]["event_id"]], "changes": {"contract_stage": "invented"}, "reason": "invalid"})
        approved = self.pipeline.review(record["issue_id"], {"revision": 1, "approval_hash": modified["content_hash"], "action": "approve", "reason": "fixture"})
        with self.assertRaises(ValueError):
            self.publish(approved)
        self.assertEqual(self.publisher.calls, [])

    def test_reconcile_bounded_new_verification_attempts(self):
        self.publisher.state = "COMMITTED"
        self.publish(self.ready())
        self.config["policy"]["auto_recover"] = True
        self.config["collection"]["enabled"] = True
        self.runner.clock = lambda: datetime(2026, 9, 29, 23, 5, tzinfo=timezone.utc)
        first = self.runner.reconcile()
        self.assertEqual(len(first["enqueued_jobs"]), 1)
        self.runner.run_once()
        second = self.runner.reconcile()
        self.assertEqual(len(second["enqueued_jobs"]), 1)
        self.assertNotEqual(first["enqueued_jobs"], second["enqueued_jobs"])

    def test_backup_async_job(self):
        self.ready()
        job_id = self.enqueue("/v1/backup", {}, "backup")
        self.runner.run_once()
        job = self.store.job(job_id)
        self.assertEqual(job["status"], "SUCCEEDED")
        self.assertTrue((Path(self.temp.name) / "backups" / job["result"]["filename"]).exists())

    def hourly(self, hour=23, minute=30, day=29):
        self.runner.clock = lambda: datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)
        return self.enqueue("/v1/briefings/check", {"issue_date": "2026-09-30"}, "ignored-hourly-key")

    def ingest_hourly_source(self, status, key, minute):
        self.pipeline.ingest({"articles": [], "source_results": [{"source_id": "fixture", "status": status, "count": 0}]}, key)
        self.store.call(lambda db: db.execute("UPDATE batches SET created_at=? WHERE batch_id=?",
            [f"2026-09-29T23:{minute:02d}:00+00:00", key]).fetchall(), write=True)

    def test_hourly_guard_auth_and_persistent_slot_idempotency(self):
        self.runner.clock = lambda: datetime(2026, 9, 29, 23, 29, tzinfo=timezone.utc)
        self.assertEqual(self.client.post("/v1/briefings/check", json={}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/v1/briefings/check", json={}).status_code, 401)
        first = self.hourly()
        self.assertEqual(self.hourly(hour=0, minute=29, day=30), first)
        self.assertNotEqual(self.hourly(hour=0, minute=30, day=30), first)
        for data in ({"issue_date": "2026-09-29"}, {"revision": 1}, {"message_type": "correction"}):
            self.assertEqual(self.client.post("/v1/briefings/check", json=data, headers=self.headers).status_code, 400)

    def test_hourly_publishes_ready_then_sends_once_and_stops_followup_jobs(self):
        record = self.ready()
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "ACCEPTED", "http_status": 202}) as send:
            first = self.hourly()
            self.runner.run_once()
            self.assertEqual(self.store.job(first)["result"]["status"], "ACCEPTED")
            with patch.object(self.pipeline, "prepare", side_effect=AssertionError("must not summarize again")):
                second = self.hourly(0, 30, 30)
                self.runner.run_once()
                self.assertFalse(self.store.job(second)["result"]["claimed"])
                queued = self.enqueue("/v1/issues/prepare", {"issue_date": record["issue_date"]}, "late-prepare")
                self.runner.run_once()
                self.assertEqual(self.store.job(queued)["result"]["revision"], 1)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(len(self.publisher.calls), 1)
            status = self.client.get("/v1/deliveries/status", headers=self.headers).json
            self.assertTrue(status["briefing_stopped"])
            self.assertEqual(status["delivery_evidence"]["http_status"], 202)
            self.assertEqual(self.runner.reconcile()["enqueued_jobs"], [])

    def test_hourly_blocked_recovery_uses_new_collection_and_no_news_is_final(self):
        self.config["collection"]["enabled"] = True
        self.config["policy"].update(automatic_publication=True, review_mode="codex_oauth")
        self.ingest_hourly_source("request_error", "failed-source", 0)
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "ACCEPTED"}) as send:
            first = self.hourly()
            self.runner.run_once()
            self.assertEqual(self.store.job(first)["result"]["error_code"], "WEB_VERIFICATION_REQUIRED")
            send.assert_not_called()
            self.ingest_hourly_source("success_zero", "healthy-source", 31)
            second = self.hourly(0, 30, 30)
            self.runner.run_once()
            self.assertEqual(self.store.job(second)["result"]["status"], "ACCEPTED")
            self.assertEqual(send.call_args.args[0]["content_status"], "no_news")
            self.assertEqual(send.call_count, 1)

    def test_unknown_preserves_diagnostics_and_never_resends_after_restart(self):
        self.publish(self.ready())
        with patch.dict(os.environ, {"WIND_NEWS_TEAMS_WEBHOOK_URL": "https://fixture.logic.azure.com/example"}), patch("news.delivery.send_card", return_value={"status": "UNKNOWN", "error_code": "teams_ambiguous_response", "http_status": 502, "response_class": "receiver_server_error"}) as send:
            first = self.hourly()
            self.runner.run_once()
            self.assertEqual(self.store.job(first)["status"], "FAILED")
            self.store.recover()
            second = self.hourly(0, 30, 30)
            self.runner.run_once()
            response = self.client.post("/v1/deliveries/send", json={"issue_date": "2026-09-30"}, headers=self.headers).json
            self.assertEqual(response["http_status"], 502)
            self.assertEqual(response["error_code"], "teams_ambiguous_response")
            self.assertFalse(response["claimed"])
            self.assertEqual(send.call_count, 1)

    def test_hourly_queued_job_does_not_publish_or_send_previous_day(self):
        self.ready()
        job = self.hourly()
        self.runner.clock = lambda: datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)
        with patch("news.delivery.send_card") as send:
            self.runner.run_once()
            self.assertEqual(self.store.job(job)["result"]["error_code"], "BRIEFING_DATE_EXPIRED")
            self.assertEqual(self.publisher.calls, [])
            send.assert_not_called()

    def test_final_hour_missing_publication_is_failure_and_day_before_is_waiting(self):
        self.config["collection"]["enabled"] = True
        self.ingest_hourly_source("request_error", "failed-source", 0)
        early = self.hourly(13, 30, 30)
        self.runner.run_once()
        self.assertEqual(self.store.job(early)["status"], "SUCCEEDED")
        final = self.hourly(14, 30, 30)
        self.runner.run_once()
        self.assertEqual(self.store.job(final)["error_code"], "PUBLICATION_NOT_READY_BY_DEADLINE")

    def test_reconcile_leaves_after_0830_recovery_to_hourly_job(self):
        self.ready()
        self.config["collection"]["enabled"] = True
        self.config["policy"]["auto_recover"] = True
        self.runner.clock = lambda: datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(self.runner.reconcile()["enqueued_jobs"], [])


if __name__ == "__main__":
    unittest.main()
