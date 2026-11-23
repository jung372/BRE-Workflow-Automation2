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

from .schedule import KST, MAINTENANCE_PAUSE, MORNING_START, SEND_DEADLINE, scheduled_job


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
                CREATE TABLE IF NOT EXISTS job_aliases (
                  operation VARCHAR, idempotency_key VARCHAR, request_hash VARCHAR, job_id VARCHAR,
                  PRIMARY KEY(operation, idempotency_key));
                CREATE TABLE IF NOT EXISTS job_schedule (
                  job_id VARCHAR PRIMARY KEY, schedule_group VARCHAR, schedule_date VARCHAR,
                  expires_at VARCHAR, recovery_attempts INTEGER NOT NULL DEFAULT 0);
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

    @property
    def alive(self):
        return not self._closed and self._thread.is_alive()

    def call(self, fn, *, write=False, timeout=120):
        if self._closed:
            raise RuntimeError("store_closed")
        future = Future()
        self._queue.put((fn, write, future))
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def bind_industry(self, industry):
        from .profiles import profile_for
        profile_for(industry)
        def bind(db):
            db.execute("CREATE TABLE IF NOT EXISTS news_profile (industry VARCHAR PRIMARY KEY)")
            existing = db.execute("SELECT industry FROM news_profile").fetchall()
            foreign = db.execute("SELECT 1 FROM issues WHERE issue_id NOT LIKE ? LIMIT 1", [industry + "-%"]).fetchone()
            legacy_data = not existing and industry == "solar" and any(
                db.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                for table in ("articles", "batches", "jobs", "events", "deliveries"))
            if foreign or legacy_data or any(row[0] != industry for row in existing):
                raise ValueError("news_database_industry_mismatch")
            if not existing:
                db.execute("INSERT INTO news_profile VALUES (?)", [industry])
        self.call(bind, write=True)

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

    def submit(self, operation, key, payload, *, schedule=None, timeout=120):
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ValueError("idempotency_key_required")
        request_hash = digest(payload)
        def work(db):
            previous = self._job(db.execute("SELECT * FROM jobs WHERE operation=? AND idempotency_key=?", [operation, key]).fetchone())
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("idempotency_payload_conflict")
                return previous
            alias = db.execute("SELECT request_hash,job_id FROM job_aliases WHERE operation=? AND idempotency_key=?", [operation, key]).fetchone()
            if alias:
                if alias[0] != request_hash:
                    raise Conflict("idempotency_payload_conflict")
                return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [alias[1]]).fetchone())
            if schedule:
                group, day, _ = schedule
                active = db.execute("""SELECT j.* FROM jobs j JOIN job_schedule s USING (job_id)
                    WHERE operation=? AND schedule_group=? AND schedule_date=?
                    AND (request_hash=? OR schedule_group IN ('collect','cutoff','reconcile','backup','purge'))
                    AND status IN ('QUEUED','RUNNING') ORDER BY created_at,job_id LIMIT 1""",
                    [operation, group, day, request_hash]).fetchone()
                if active:
                    db.execute("INSERT INTO job_aliases VALUES (?,?,?,?)", [operation, key, request_hash, active[0]])
                    return self._job(active)
            job_id, now = str(uuid.uuid4()), utcnow()
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,'QUEUED',NULL,NULL,?,?)", [job_id, operation, key, request_hash, canonical(payload), now, now])
            if schedule:
                db.execute("INSERT INTO job_schedule (job_id,schedule_group,schedule_date,expires_at) VALUES (?,?,?,?)", [job_id, *schedule])
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id]).fetchone())
        return self.call(work, write=True, timeout=timeout)

    def job(self, job_id):
        return self.call(lambda db: self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [job_id]).fetchone()))

    @staticmethod
    def _expire_jobs(db, now):
        local_day = datetime.fromisoformat(now).astimezone(KST).date().isoformat()
        db.execute("""UPDATE jobs SET status=CASE WHEN schedule_group='briefing' AND schedule_date<$day THEN 'SUCCEEDED' ELSE 'FAILED' END,
            error_code=CASE WHEN schedule_group<>'briefing' THEN 'SCHEDULE_EXPIRED'
                WHEN schedule_date=$day THEN 'BRIEFING_DELIVERY_DEADLINE_MISSED' END,
            result=CASE WHEN schedule_group='briefing' AND schedule_date<$day THEN $expired END,updated_at=$now
            FROM job_schedule s WHERE jobs.job_id=s.job_id AND status='QUEUED' AND expires_at<=$now""",
            {'day': local_day, 'expired': canonical({'status': 'SKIPPED', 'error_code': 'BRIEFING_DATE_EXPIRED'}), 'now': now})

    def next_job(self, now=None, *, protect_morning=False):
        def work(db):
            local = now.astimezone(KST) if now else None
            protected = bool(protect_morning and local and MAINTENANCE_PAUSE <= local.time() < SEND_DEADLINE)
            morning = bool(protected and local.time() >= MORNING_START)
            if now:
                self._expire_jobs(db, now.astimezone(timezone.utc).isoformat())
            row = db.execute("""SELECT j.* FROM jobs j LEFT JOIN job_schedule s USING (job_id) WHERE status='QUEUED'
                AND (NOT ? OR operation NOT IN ('backup','purge'))
                AND (NOT ? OR schedule_date=? AND schedule_group IN ('cutoff','prepare','publish','reconcile','briefing'))
                ORDER BY CASE schedule_group WHEN 'briefing' THEN 0 WHEN 'cutoff' THEN 1 WHEN 'prepare' THEN 2
                    WHEN 'publish' THEN 3 WHEN 'reconcile' THEN 4 ELSE 5 END, created_at,job_id LIMIT 1""",
                [protected, morning, local.date().isoformat() if local else None]).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='RUNNING',updated_at=? WHERE job_id=?", [utcnow(), row[0]])
            return self._job(db.execute("SELECT * FROM jobs WHERE job_id=?", [row[0]]).fetchone())
        return self.call(work, write=True, timeout=None)

    def finish_job(self, job_id, result=None, error_code=None):
        def work(db):
            operation = db.execute("SELECT operation FROM jobs WHERE job_id=?", [job_id]).fetchone()
            db.execute("UPDATE jobs SET status=?,result=?,error_code=?,updated_at=? WHERE job_id=?", ["FAILED" if error_code else "SUCCEEDED", canonical(result) if result is not None else None, error_code, utcnow(), job_id])
            if operation and operation[0] == "ingest" and not error_code:
                db.execute("UPDATE jobs SET payload=? WHERE job_id=?", [canonical({"batch_id": job_id}), job_id])
        self.call(work, write=True, timeout=None)

    def recover(self, now=None):
        stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

        def work(db):
            for job_id, operation, key, payload, created in db.execute("""SELECT job_id,operation,idempotency_key,payload,created_at
                FROM jobs LEFT JOIN job_schedule USING (job_id)
                WHERE status IN ('QUEUED','RUNNING') AND schedule_group IS NULL
                AND regexp_matches(idempotency_key, '^(BRE-(WIND|SOLAR)-|morning-briefing-|recovery-)')""").fetchall():
                schedule = scheduled_job(operation, key, json.loads(payload), datetime.fromisoformat(created))
                if schedule:
                    db.execute("""INSERT INTO job_schedule (job_id,schedule_group,schedule_date,expires_at) VALUES (?,?,?,?)
                        ON CONFLICT (job_id) DO UPDATE SET schedule_group=excluded.schedule_group,
                        schedule_date=excluded.schedule_date,expires_at=excluded.expires_at""", [job_id, *schedule])
            db.execute("""UPDATE jobs SET status='FAILED',error_code='WORKER_RECOVERY_LIMIT',updated_at=?
                WHERE status='RUNNING' AND job_id IN (SELECT job_id FROM job_schedule WHERE recovery_attempts>=2)""", [stamp])
            db.execute("""INSERT INTO job_schedule (job_id,recovery_attempts)
                SELECT job_id,1 FROM jobs WHERE status='RUNNING'
                ON CONFLICT (job_id) DO UPDATE SET recovery_attempts=job_schedule.recovery_attempts+1""")
            jobs = [row[0] for row in db.execute("""UPDATE jobs SET status='QUEUED',updated_at=?
                WHERE status='RUNNING' RETURNING job_id""", [stamp]).fetchall()]
            self._expire_jobs(db, stamp)
            jobs = [job_id for job_id in jobs if db.execute("SELECT status FROM jobs WHERE job_id=?", [job_id]).fetchone()[0] == 'QUEUED']
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

    def claim_delivery(self, issue_id, revision, channel_id, message_type="daily", *, max_attempts=3):
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 12:
            raise ValueError("invalid_delivery_attempt_limit")
        if not channel_id or len(channel_id) > 100 or message_type not in {"daily", "correction", "connection_test"}:
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
                if previous[0]["status"] == "FAILED" and (previous[0]["result"] or {}).get("attempt", 1) < max_attempts:
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

    def pending_deliveries(self, channel_id="wind-news", *, max_attempts=3):
        def work(db):
            result = []
            for row in db.execute("SELECT * FROM issues WHERE state='WEB_VERIFIED' QUALIFY "
                                  "row_number() OVER (PARTITION BY issue_date ORDER BY revision DESC)=1 ORDER BY issue_date").fetchall():
                issue = self._issue(row)
                kind = "daily"
                daily = self._daily_delivery(db, issue["issue_date"], channel_id)
                existing = [daily] if daily else []
                if not existing or existing[0]["status"] == "FAILED" and (existing[0]["result"] or {}).get("attempt", 1) < max_attempts:
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
        self.call(work, timeout=None)
        with destination.open("rb") as stream:
            return {"filename": destination.name, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
