"""Persistent single-process DuckDB store with one serialized writer queue.

Every read and write uses the owning thread's connection. No HTTP worker opens
another writable connection. DuckDB's OS lock prevents a second service process.
Published snapshots and editorial history have no automatic expiration.
"""
from concurrent.futures import Future
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import queue
import shutil
import threading
import uuid
import os

import duckdb


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Conflict(ValueError):
    """A stale revision/hash or reused idempotency key."""


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = queue.Queue()
        self._closed = False
        self._ready = Future()
        self._thread = threading.Thread(target=self._writer, name="wind-news-db", daemon=True)
        self._thread.start()
        self._ready.result(timeout=30)

    def _writer(self):
        connection = None
        try:
            connection = duckdb.connect(str(self.path))
            connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id VARCHAR PRIMARY KEY, operation VARCHAR NOT NULL,
                  idempotency_key VARCHAR NOT NULL, request_hash VARCHAR NOT NULL,
                  payload VARCHAR NOT NULL, status VARCHAR NOT NULL,
                  result VARCHAR, error_code VARCHAR, created_at VARCHAR, updated_at VARCHAR,
                  UNIQUE(operation, idempotency_key));
                CREATE TABLE IF NOT EXISTS batches (
                  batch_id VARCHAR PRIMARY KEY, created_at VARCHAR, source_results VARCHAR);
                CREATE TABLE IF NOT EXISTS articles (
                  article_id VARCHAR PRIMARY KEY, canonical_url VARCHAR UNIQUE,
                  content_hash VARCHAR, metadata VARCHAR, evidence VARCHAR,
                  discovered_at VARCHAR, updated_at VARCHAR);
                CREATE TABLE IF NOT EXISTS batch_articles (
                  batch_id VARCHAR, article_id VARCHAR, article_snapshot VARCHAR,
                  PRIMARY KEY(batch_id, article_id));
                CREATE TABLE IF NOT EXISTS events (
                  event_id VARCHAR PRIMARY KEY, event_key VARCHAR UNIQUE, metadata VARCHAR);
                CREATE TABLE IF NOT EXISTS event_articles (
                  event_id VARCHAR, article_id VARCHAR, decision VARCHAR,
                  PRIMARY KEY(event_id, article_id));
                CREATE TABLE IF NOT EXISTS issues (
                  issue_id VARCHAR, revision INTEGER, issue_date VARCHAR, state VARCHAR,
                  content_hash VARCHAR, approval_hash VARCHAR, payload VARCHAR,
                  candidates VARCHAR, batch_ids VARCHAR, publish_result VARCHAR,
                  created_at VARCHAR, updated_at VARCHAR,
                  PRIMARY KEY(issue_id, revision));
                CREATE TABLE IF NOT EXISTS editorial_actions (
                  action_id VARCHAR PRIMARY KEY, issue_id VARCHAR, revision INTEGER,
                  action VARCHAR, actor VARCHAR, reason VARCHAR,
                  before_hash VARCHAR, after_hash VARCHAR, created_at VARCHAR);
                CREATE TABLE IF NOT EXISTS deliveries (
                  delivery_id VARCHAR PRIMARY KEY, issue_id VARCHAR, revision INTEGER,
                  channel_id VARCHAR, message_type VARCHAR, status VARCHAR,
                  claimed_at VARCHAR, updated_at VARCHAR, result VARCHAR,
                  UNIQUE(issue_id, revision, channel_id, message_type));
            """)
            connection.execute("ALTER TABLE batch_articles ADD COLUMN IF NOT EXISTS article_snapshot VARCHAR")
            self._ready.set_result(True)
            while True:
                task = self._queue.get()
                if task is None:
                    break
                fn, transactional, future = task
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if transactional:
                        connection.execute("BEGIN TRANSACTION")
                    result = fn(connection)
                    if transactional:
                        connection.execute("COMMIT")
                    future.set_result(result)
                except BaseException as exc:
                    if transactional:
                        connection.execute("ROLLBACK")
                    future.set_exception(exc)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            if connection is not None:
                connection.close()

    def call(self, fn, *, write=False):
        if self._closed:
            raise RuntimeError("store_closed")
        future = Future()
        self._queue.put((fn, write, future))
        return future.result(timeout=120)

    def close(self):
        if not self._closed:
            self._closed = True
            self._queue.put(None)
            self._thread.join(timeout=30)

    @staticmethod
    def _job(row):
        if not row:
            return None
        keys = ("job_id", "operation", "idempotency_key", "request_hash", "payload", "status", "result", "error_code", "created_at", "updated_at")
        result = dict(zip(keys, row))
        for key in ("payload", "result"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def submit(self, operation, key, payload):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("idempotency_key_required")
        request_hash = digest(payload)
        def work(db):
            previous = self._job(db.execute("SELECT * FROM jobs WHERE operation=? AND idempotency_key=?", [operation, key]).fetchone())
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("idempotency_payload_conflict")
                return previous
            job_id, now = str(uuid.uuid4()), utcnow()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,'QUEUED',NULL,NULL,?,?)", [job_id, operation, key, request_hash, canonical(payload), now, now])
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id]).fetchone())
        return self.call(work, write=True)

    def job(self, job_id):
        return self.call(lambda db: self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id]).fetchone()))

    def next_job(self):
        def work(db):
            row = db.execute("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='RUNNING',updated_at=? WHERE job_id=?", [utcnow(), row[0]])
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [row[0]]).fetchone())
        return self.call(work, write=True)

    def finish_job(self, job_id, result=None, error_code=None):
        def work(db):
            operation = db.execute("SELECT operation FROM jobs WHERE job_id=?", [job_id]).fetchone()
            db.execute("UPDATE jobs SET status=?,result=?,error_code=?,updated_at=? WHERE job_id=?", ["FAILED" if error_code else "SUCCEEDED", canonical(result) if result is not None else None, error_code, utcnow(), job_id])
            if operation and operation[0] == "ingest" and not error_code:
                db.execute("UPDATE jobs SET payload=? WHERE job_id=?", [canonical({"batch_id": job_id}), job_id])
        self.call(work, write=True)

    def recover(self):
        def work(db):
            jobs = [row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status='RUNNING'").fetchall()]
            db.execute("UPDATE jobs SET status='QUEUED',updated_at=? WHERE status='RUNNING'", [utcnow()])
            db.execute("UPDATE issues SET state='READY',updated_at=? WHERE state='PUBLISHING'", [utcnow()])
            # Sending may have succeeded immediately before a crash. Never reclaim.
            db.execute("UPDATE deliveries SET status='UNKNOWN',updated_at=? WHERE status='CLAIMED'", [utcnow()])
            return {"requeued_jobs": jobs, "unknown_deliveries": self._deliveries(db, "status='UNKNOWN'")}
        return self.call(work, write=True)

    @staticmethod
    def _issue(row):
        if not row:
            return None
        keys = ("issue_id", "revision", "issue_date", "state", "content_hash", "approval_hash", "payload", "candidates", "batch_ids", "publish_result", "created_at", "updated_at")
        result = dict(zip(keys, row))
        for key in ("payload", "candidates", "batch_ids", "publish_result"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def issue(self, issue_id, revision=None):
        def work(db):
            if revision is None:
                row = db.execute("SELECT * FROM issues WHERE issue_id=? ORDER BY revision DESC LIMIT 1", [issue_id]).fetchone()
            else:
                row = db.execute("SELECT * FROM issues WHERE issue_id=? AND revision=?", [issue_id, revision]).fetchone()
            return self._issue(row)
        return self.call(work)

    def list_issues(self, published=False):
        where = "WHERE state IN ('COMMITTED','WEB_VERIFIED')" if published else ""
        return self.call(lambda db: [self._issue(row) for row in db.execute("SELECT * FROM issues " + where + " ORDER BY issue_date,revision").fetchall()])

    def verified_issue(self, issue_id):
        return self.call(lambda db: self._issue(db.execute(
            "SELECT * FROM issues WHERE issue_id=? AND state='WEB_VERIFIED' ORDER BY revision DESC LIMIT 1",
            [issue_id]).fetchone()))

    def daily_delivery(self, issue_date, channel_id="wind-news"):
        return self.call(lambda db: self._daily_delivery(db, issue_date, channel_id))

    @classmethod
    def _daily_delivery(cls, db, issue_date, channel_id):
        previous = cls._deliveries(db, "message_type='daily' AND channel_id=? AND (issue_id,revision) IN "
            "(SELECT issue_id,revision FROM issues WHERE issue_date=?)", [channel_id, issue_date])
        # Preserve ambiguous/accepted historical sends even if another retry failed.
        return next((d for d in previous if d["status"] != "FAILED"), previous[-1] if previous else None)

    @staticmethod
    def _deliveries(db, where="1=1", params=None):
        keys = ("delivery_id", "issue_id", "revision", "channel_id", "message_type", "status", "claimed_at", "updated_at", "result")
        result = []
        for row in db.execute("SELECT * FROM deliveries WHERE " + where + " ORDER BY updated_at", params or []).fetchall():
            item = dict(zip(keys, row))
            item["result"] = json.loads(item["result"]) if item["result"] else None
            result.append(item)
        return result

    def claim_delivery(self, issue_id, revision, channel_id, message_type="daily"):
        if not channel_id or len(channel_id) > 100 or message_type not in {"daily", "correction"}:
            raise ValueError("invalid_delivery_key")
        def work(db):
            issue = self._issue(db.execute("SELECT * FROM issues WHERE issue_id=? AND revision=?", [issue_id, revision]).fetchone())
            if not issue or issue["state"] != "WEB_VERIFIED":
                raise Conflict("web_verification_required")
            if message_type == "daily":
                daily = self._daily_delivery(db, issue["issue_date"], channel_id)
                previous = [daily] if daily else []
            else:
                previous = self._deliveries(db, "issue_id=? AND revision=? AND channel_id=? AND message_type=?", [issue_id, revision, channel_id, message_type])
            if previous:
                if previous[0]["status"] == "FAILED" and (previous[0]["result"] or {}).get("attempt", 1) < 3:
                    now = utcnow()
                    attempt = (previous[0]["result"] or {}).get("attempt", 1) + 1
                    db.execute("UPDATE deliveries SET issue_id=?,revision=?,status='CLAIMED',claimed_at=?,updated_at=?,result=? WHERE delivery_id=?", [issue_id, revision, now, now, canonical({"attempt": attempt}), previous[0]["delivery_id"]])
                    return {**self._deliveries(db, "delivery_id=?", [previous[0]["delivery_id"]])[0], "claimed": True}
                return {**previous[0], "claimed": False}
            now, delivery_id = utcnow(), str(uuid.uuid4())
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,'CLAIMED',?,?,NULL)", [delivery_id, issue_id, revision, channel_id, message_type, now, now])
            return {**self._deliveries(db, "delivery_id=?", [delivery_id])[0], "claimed": True}
        return self.call(work, write=True)

    def delivery_result(self, delivery_id, status, evidence=None):
        if status not in {"ACCEPTED", "DELIVERED", "FAILED", "UNKNOWN"}:
            raise ValueError("invalid_delivery_status")
        evidence = evidence or {}
        if status == "DELIVERED" and not evidence.get("message_id"):
            raise ValueError("message_evidence_required")
        def work(db):
            previous = self._deliveries(db, "delivery_id=?", [delivery_id])
            if not previous:
                raise ValueError("delivery_not_found")
            allowed = {"CLAIMED": {"ACCEPTED", "DELIVERED", "FAILED", "UNKNOWN"}, "ACCEPTED": {"DELIVERED", "UNKNOWN"}, "UNKNOWN": {"DELIVERED"}}
            if previous[0]["status"] != status and status not in allowed.get(previous[0]["status"], set()):
                raise Conflict("invalid_delivery_transition")
            result = {**evidence, "attempt": (previous[0]["result"] or {}).get("attempt", 1)}
            db.execute("UPDATE deliveries SET status=?,result=?,updated_at=? WHERE delivery_id=?", [status, canonical(result), utcnow(), delivery_id])
            return self._deliveries(db, "delivery_id=?", [delivery_id])[0]
        return self.call(work, write=True)

    def pending_deliveries(self, channel_id="wind-news"):
        def work(db):
            result = []
            for row in db.execute("SELECT * FROM issues WHERE state='WEB_VERIFIED' QUALIFY "
                                  "row_number() OVER (PARTITION BY issue_date ORDER BY revision DESC)=1 ORDER BY issue_date").fetchall():
                issue = self._issue(row)
                kind = "daily"
                daily = self._daily_delivery(db, issue["issue_date"], channel_id)
                existing = [daily] if daily else []
                if not existing or existing[0]["status"] == "FAILED" and (existing[0]["result"] or {}).get("attempt", 1) < 3:
                    result.append({"issue_id": issue["issue_id"], "revision": issue["revision"], "channel_id": channel_id, "message_type": kind})
            return result
        return self.call(work)

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path or destination.exists():
            raise ValueError("backup_destination_must_be_new")
        destination.parent.mkdir(parents=True, exist_ok=True)
        def work(db):
            db.execute("CHECKPOINT")
            temporary = destination.with_name(destination.name + ".partial-" + str(uuid.uuid4()))
            # Windows locks an open DuckDB handle against raw file copying.
            # Native COPY executes while this sole writer queue is paused.
            source_database = db.execute("SELECT current_database()").fetchone()[0]
            literal_path = str(temporary).replace("'", "''")
            source_identifier = '"' + source_database.replace('"', '""') + '"'
            db.execute("ATTACH '" + literal_path + "' AS wind_news_backup")
            try:
                db.execute("COPY FROM DATABASE " + source_identifier + " TO wind_news_backup")
            finally:
                db.execute("DETACH wind_news_backup")
            verification = duckdb.connect(str(temporary), read_only=True)
            try:
                verification.execute("SELECT count(*) FROM issues").fetchone()
            finally:
                verification.close()
            os.replace(temporary, destination)
            return {"filename": destination.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        return self.call(work)
