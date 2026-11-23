"""Authenticated internal API. Persistent jobs precede 202 responses."""
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import re
import threading
import time

from flask import Flask, jsonify, render_template, request

from .profiles import profile_for, bind_config
from .pipeline import KST, SUCCESS, Pipeline, approval_digest, window
from .schedule import FIRST_SEND, MAINTENANCE_PAUSE, MORNING_START, PURGE_LAST_START, SEND_DEADLINE, scheduled_job, slot
from .store import Conflict, Store, canonical, utcnow


def load_config(path=None, industry="wind"):
    profile_for(industry)
    if path:
        with Path(path).open(encoding="utf-8") as stream:
            config = json.load(stream)
        if "collection" not in config or "policy" not in config:
            raise ValueError("config_requires_collection_and_policy")
        return config
    root = Path(__file__).resolve().parents[1] / "config"
    return {"collection": json.loads((root / f"{industry}_news_collection.json").read_text(encoding="utf-8")), "policy": json.loads((root / f"{industry}_news_policy.json").read_text(encoding="utf-8"))}


def present_issue(record):
    if record is None:
        return None
    result = dict(record)
    result["issue"] = result.pop("payload")
    return result


class JobRunner:
    JOB_LIMIT_SECONDS = {'backup': 1200, 'purge': 600, 'collect': 600, 'prepare': 1200, 'publish': 360, 'briefing': 1500}

    def __init__(self, store, pipeline, config, runtime, collector=None, publisher=None, clock=None):
        self.store, self.pipeline, self.config = store, pipeline, config
        self.profile = profile_for(config.get("industry", "wind"))
        self.runtime = Path(runtime)
        self.collector, self.publisher = collector, publisher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._phase = ('queue', time.monotonic())
        self.delivery_lock = threading.Lock()

    def briefing_stopped(self, day):
        delivery = self.store.daily_delivery(day, self.profile.channel)
        return delivery if delivery and (delivery["status"] in {"CLAIMED", "ACCEPTED", "DELIVERED", "UNKNOWN"}
            or delivery["status"] == "FAILED" and (delivery.get("result") or {}).get("attempt", 1) >= 12) else None

    def start(self):
        self.store.recover(self.clock())
        self._thread = threading.Thread(target=self._loop, name="wind-news-jobs", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=35)

    def _loop(self):
        last_slot = None
        while not self._stop.is_set():
            self._phase = ('queue', time.monotonic())
            now = self.clock().astimezone(KST)
            if slot(now) != last_slot:
                self.schedule_local(now)
                last_slot = slot(now)
            if not self.run_once():
                self._wake.wait(1)
                self._wake.clear()

    def failure(self):
        if not self._thread or not self._thread.is_alive():
            return 'NEWS_JOB_WORKER_STOPPED'
        if not self.store.alive:
            return 'NEWS_DB_WRITER_STOPPED'
        operation, started = self._phase
        if time.monotonic() - started >= self.JOB_LIMIT_SECONDS.get(operation, 90):
            return 'NEWS_JOB_PROGRESS_TIMEOUT'
        return None

    def submit_scheduled(self, operation, group, payload, now, *, stamp=None):
        key = 'news-schedule:' + group + ':' + (stamp or slot(now))
        return self.store.submit(operation, key, payload, schedule=scheduled_job(operation, key, payload, now), timeout=30)

    def schedule_local(self, now):
        # n8n still owns Teams delivery. This only restores the pre-send stages.
        if self.config['policy'].get('auto_recover') and MORNING_START <= now.time() < FIRST_SEND:
            self.submit_scheduled('reconcile', 'reconcile', {}, now)
        if now.time() < PURGE_LAST_START or now.time() >= SEND_DEADLINE:
            self.submit_scheduled('purge', 'purge', {}, now, stamp=now.date().isoformat())

    def run_once(self):
        job = self.store.next_job(self.clock(), protect_morning=bool(self.config['policy'].get('auto_recover')))
        if not job:
            return False
        self._phase = (job['operation'], time.monotonic())
        try:
            result = self.execute(job)
        except (Conflict, ValueError) as exc:
            code = str(exc) if re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(exc)) else "VALIDATION_FAILED"
            self.store.finish_job(job["job_id"], error_code=code)
        except Exception:
            # No exception/URL/header text is copied to the API or persistent job.
            self.store.finish_job(job["job_id"], error_code="INTERNAL_JOB_ERROR")
        else:
            self.store.finish_job(job["job_id"], result=result)
        return True

    def execute(self, job):
        operation, payload = job["operation"], job["payload"]
        if operation == "ingest":
            return self.pipeline.ingest(payload, job["job_id"])
        if operation == "collect":
            return self.collect(job['job_id'])
        if operation == "prepare":
            if self.briefing_stopped(payload["issue_date"]) and not payload.get("correction_reason"):
                return present_issue(self.store.verified_issue(self.profile.industry + "-" + payload["issue_date"]))
            now = self.clock().astimezone(KST)
            if (self.config['policy'].get('auto_recover') and self.config['collection'].get('enabled')
                    and payload['issue_date'] == now.date().isoformat() and MORNING_START <= now.time() < FIRST_SEND
                    and not payload.get('correction_reason') and scheduled_job(operation, job['idempotency_key'], payload, now)
                    and not self.store.issue(self.profile.issue_id(payload['issue_date'])) and not self.cutoff_batch(payload['issue_date'])):
                self.collect(job['job_id'] + '-cutoff')
            return present_issue(self.pipeline.prepare(payload))
        if operation == "briefing":
            return self.check_briefing(payload, job_id=job['job_id'])
        if operation == "publish":
            return self.publish(payload)
        if operation == "backup":
            destination = self.runtime / "backups" / (self.profile.industry + "-news-" + job["job_id"] + ".duckdb")
            if destination.exists():
                import hashlib
                with destination.open("rb") as stream:
                    return {"filename": destination.name, "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
            return self.store.backup(destination)
        if operation == 'purge':
            return {'expired_evidence_cleaned': self.pipeline.purge_expired_evidence(
                continue_work=lambda: not MAINTENANCE_PAUSE <= self.clock().astimezone(KST).time() < SEND_DEADLINE)}
        if operation == "reconcile":
            return self.reconcile()
        raise ValueError("unsupported_job_operation")

    def collect(self, batch_id):
        from .collector import Collector
        collector = self.collector or Collector(self.config['collection'])
        until = self.clock()
        since = until - timedelta(hours=max(72, self.config['collection'].get('overlap_hours', 72)))
        batch = collector.collect(since=since, until=until)
        return self.pipeline.ingest(batch, batch_id)

    def cutoff_batch(self, day):
        _, cutoff = window(day)
        start = cutoff.astimezone(timezone.utc)
        stop = (cutoff.replace(hour=0, minute=0) + timedelta(days=1)).astimezone(timezone.utc)
        row = self.store.call(lambda db: db.execute("""SELECT batch_id,source_results FROM batches
            WHERE created_at>=? AND created_at<? ORDER BY created_at DESC,batch_id DESC LIMIT 1""",
            [start.isoformat(), stop.isoformat()]).fetchone())
        required = {s['source_id'] for s in self.config['collection'].get('sources', []) if s.get('enabled', True) and s.get('required')}
        if row and required and required <= {s['source_id'] for s in json.loads(row[1]) if s.get('status') in SUCCESS}:
            return row[0]
        return None

    def publish(self, payload):
        issue_id, revision = payload["issue_id"], payload["revision"]
        record = self.store.issue(issue_id, revision)
        if not record:
            raise ValueError("issue_not_found")
        if record["approval_hash"] != payload.get("approval_hash") or record["approval_hash"] != record["content_hash"]:
            raise Conflict("stale_approval_hash")
        if approval_digest(record["payload"], record["candidates"], record["batch_ids"]) != record["approval_hash"]:
            raise Conflict("approved_content_changed")
        if record["state"] == "WEB_VERIFIED":
            return present_issue(record)
        if self.briefing_stopped(record["issue_date"]):
            raise Conflict("daily_briefing_already_finished")
        if record["state"] not in {"READY", "COMMITTED"}:
            raise Conflict("issue_not_ready")
        if not self.config["policy"].get("publisher", {}).get("enabled"):
            raise ValueError("publication_disabled")
        from .publisher import Publisher
        publisher = self.publisher or Publisher(self.config["policy"]["publisher"])
        def freeze(db):
            current = self.store._issue(db.execute("SELECT * FROM issues WHERE issue_id=? AND revision=?", [issue_id, revision]).fetchone())
            if current["state"] not in {"READY", "COMMITTED"} or current["content_hash"] != record["content_hash"]:
                raise Conflict("publication_state_changed")
            public = current["payload"]
            if public["published_at"] is None:
                public["published_at"] = utcnow()
            from jsonschema import Draft202012Validator, FormatChecker
            schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / f"{self.profile.industry}-news-issue.json").read_text(encoding="utf-8"))
            export = {**public, "coverage": {k: v for k, v in public["coverage"].items() if k in {"expected_sources", "successful_sources", "partial"}}}
            if list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(export)):
                raise ValueError("public_issue_schema_invalid")
            db.execute("UPDATE issues SET state='PUBLISHING',payload=?,updated_at=? WHERE issue_id=? AND revision=?", [canonical(public), utcnow(), issue_id, revision])
        self.store.call(freeze, write=True)
        issues = self.store.list_issues(published=True)
        issues = [i for i in issues if (i["issue_id"], i["revision"]) != (issue_id, revision)]
        issues.append(self.store.issue(issue_id, revision))
        inputs = [{**i["payload"], "state": "READY" if i["state"] == "PUBLISHING" else i["state"], "approved": True} for i in issues]
        try:
            result = publisher.publish(inputs)
            if result.get("state") not in {"COMMITTED", "WEB_VERIFIED"}:
                raise ValueError("invalid_publication_result")
        except Exception:
            # Retry the identical frozen snapshot. Publisher recognizes its own
            # pending commit, including push/result-storage crash windows.
            self.store.call(lambda db: db.execute("UPDATE issues SET state=?,updated_at=? WHERE issue_id=? AND revision=? AND state='PUBLISHING'", [record["state"], utcnow(), issue_id, revision]).fetchall(), write=True)
            raise
        safe_result = {k: result[k] for k in ("state", "commit_sha", "snapshot_id", "descriptor", "web_verified_at") if k in result}
        if result["state"] == "WEB_VERIFIED":
            safe_result["web_verified_at"] = utcnow()
        self.store.call(lambda db: db.execute("UPDATE issues SET state=?,publish_result=?,updated_at=? WHERE issue_id=? AND revision=?", [result["state"], canonical(safe_result), utcnow(), issue_id, revision]).fetchall(), write=True)
        return present_issue(self.store.issue(issue_id, revision))

    def reconcile(self):
        policy = self.config["policy"]
        now = self.clock().astimezone(KST)
        enqueued = []
        # After 08:30 the five-minute briefing job owns recovery and delivery.
        if (policy.get("auto_recover", False) and self.config["collection"].get("enabled", False)
                and MORNING_START <= now.time() < FIRST_SEND and not self.briefing_stopped(now.date().isoformat())):
            day = now.date().isoformat()
            current = self.store.issue(self.profile.industry + "-" + day)
            if not current or current['state'] == 'BLOCKED':
                if not self.cutoff_batch(day):
                    job = self.submit_scheduled('collect', 'cutoff', {}, now)
                    enqueued.append(job['job_id'])
                elif (now.hour, now.minute) >= (7, 40):
                    job = self.submit_scheduled('prepare', 'prepare', {'issue_date': day, 'retry_blocked': True}, now)
                    enqueued.append(job['job_id'])
            elif current and current["state"] in {"READY", "COMMITTED"} and (now.hour, now.minute) >= (7, 50) and policy.get("publisher", {}).get("enabled"):
                job = self.submit_scheduled('publish', 'publish', {'issue_id': current['issue_id'], 'revision': current['revision'],
                                            'approval_hash': current['approval_hash']}, now)
                enqueued.append(job['job_id'])
        return {"enqueued_jobs": enqueued, "pending_deliveries": self.store.pending_deliveries(self.profile.channel, max_attempts=12), "pending_web_verifications": [{"issue_id": i["issue_id"], "revision": i["revision"]} for i in self.store.list_issues() if i["state"] == "COMMITTED"], "unknown_deliveries": self.store.call(lambda db: self.store._deliveries(db, "status='UNKNOWN'"))}

    def check_briefing(self, payload, *, job_id=None):
        day = payload["issue_date"]
        now = self.clock().astimezone(KST)
        if day != now.date().isoformat():
            return {"status": "SKIPPED", "error_code": "BRIEFING_DATE_EXPIRED"}
        if (now.hour, now.minute) < (8, 30):
            return {"status": "SKIPPED", "error_code": "OUTSIDE_DELIVERY_WINDOW"}
        stopped = self.briefing_stopped(day)
        if stopped:
            return {"status": stopped["status"], "claimed": False}
        if (now.hour, now.minute) >= (9, 30):
            raise ValueError("BRIEFING_DELIVERY_DEADLINE_MISSED")
        issue_id = self.profile.industry + "-" + day
        record = self.store.verified_issue(issue_id)
        if not record:
            record = self.store.issue(issue_id)
            if not record or record["state"] == "BLOCKED":
                if not self.config["collection"].get("enabled", False):
                    return {"status": "SKIPPED", "error_code": "COLLECTION_DISABLED"}
                if job_id and self.config['policy'].get('auto_recover') and not self.cutoff_batch(day):
                    self.collect(job_id + '-cutoff')
                record = self.pipeline.prepare({"issue_date": day, "retry_blocked": True})
            if self.clock().astimezone(KST).date().isoformat() != day:
                return {"status": "SKIPPED", "error_code": "BRIEFING_DATE_EXPIRED"}
            after_prepare = self.clock().astimezone(KST)
            if (after_prepare.hour, after_prepare.minute) >= (9, 30):
                raise ValueError("BRIEFING_DELIVERY_DEADLINE_MISSED")
            if record["state"] in {"READY", "COMMITTED"}:
                record = self.publish({"issue_id": issue_id, "revision": record["revision"],
                                       "approval_hash": record["approval_hash"]})
        # Recheck the date after potentially long preparation/publication.
        if self.clock().astimezone(KST).date().isoformat() != day:
            return {"status": "SKIPPED", "error_code": "BRIEFING_DATE_EXPIRED"}
        outcome = self.send_delivery({"issue_date": day, "scheduled": True, "message_type": "daily"})
        if outcome["status"] in {"FAILED", "UNKNOWN"} or outcome.get("error_code") in {
                "PUBLICATION_NOT_READY_BY_DEADLINE", "BRIEFING_DELIVERY_DEADLINE_MISSED", "TEAMS_NOT_CONFIGURED"}:
            raise ValueError(outcome.get("error_code") or "BRIEFING_DELIVERY_FAILED")
        return outcome

    def send_delivery(self, data):
        now = self.clock().astimezone(KST)
        scheduled = data.get("scheduled", False)
        if not isinstance(scheduled, bool):
            raise ValueError("scheduled_must_be_boolean")
        kind = data.get("message_type", "daily")
        if data.get("channel_id", self.profile.channel) != self.profile.channel:
            raise ValueError("delivery_channel_mismatch")
        issue_id = data.get("issue_id") or self.profile.industry + "-" + data.get("issue_date", now.date().isoformat())
        if not issue_id.startswith(self.profile.industry + "-"):
            raise ValueError("issue_industry_mismatch")
        if kind == "correction" and self.briefing_stopped(issue_id.removeprefix(self.profile.industry + "-")):
            raise Conflict("daily_briefing_already_finished")
        if scheduled:
            if issue_id != self.profile.industry + "-" + now.date().isoformat() or kind != "daily" or "revision" in data:
                raise ValueError("scheduled_delivery_requires_today_latest_daily")
            if (now.hour, now.minute) < (8, 30):
                return {"status": "SKIPPED", "error_code": "OUTSIDE_DELIVERY_WINDOW"}
            if (now.hour, now.minute) >= (9, 30):
                return {"status": "SKIPPED", "error_code": "BRIEFING_DELIVERY_DEADLINE_MISSED"}
        record = (self.store.verified_issue(issue_id) if data.get("revision") is None
                  else self.store.issue(issue_id, data["revision"]))
        if not record or record["state"] != "WEB_VERIFIED":
            code = "PUBLICATION_NOT_READY_BY_DEADLINE" if scheduled and (now.hour, now.minute) >= (9, 25) else "WEB_VERIFICATION_REQUIRED"
            return {"status": "SKIPPED", "error_code": code}
        webhook = self.profile.env("TEAMS_WEBHOOK_URL")
        if not webhook:
            return {"status": "SKIPPED", "error_code": "TEAMS_NOT_CONFIGURED"}
        with self.delivery_lock:
            # A queued request may wait on another sender or DB operation.
            dispatch_time = self.clock().astimezone(KST)
            if scheduled and (dispatch_time.date().isoformat() != now.date().isoformat()
                              or (dispatch_time.hour, dispatch_time.minute) >= (9, 30)):
                return {"status": "SKIPPED", "error_code": "BRIEFING_DELIVERY_DEADLINE_MISSED"}
            claimed = self.store.claim_delivery(issue_id, record["revision"], self.profile.channel, kind,
                                                max_attempts=12 if kind == "daily" else 3)
            if not claimed["claimed"]:
                evidence = claimed.get("result") or {}
                return {"status": claimed["status"], "delivery_id": claimed["delivery_id"], "claimed": False,
                        **{k: evidence[k] for k in ("error_code", "http_status", "response_class", "receiver_error_code") if k in evidence}}
            from .delivery import send_card
            try:
                outcome = send_card(record["payload"], webhook, self.config["policy"].get("publisher", {}).get("pages_base_url", ""),
                                    card_format=self.profile.env("TEAMS_CARD_FORMAT", "adaptive"), industry=self.profile.industry,
                                    connection_test=kind == "connection_test")
                state = outcome.get("status", "UNKNOWN")
                if state == "SKIPPED":
                    state = "FAILED"
                self.store.delivery_result(claimed["delivery_id"], state,
                    {k: outcome[k] for k in ("error_code", "http_status", "response_class", "receiver_error_code") if k in outcome})
            except Exception:
                self.store.delivery_result(claimed["delivery_id"], "UNKNOWN", {"error_code": "SEND_RESULT_UNKNOWN"})
                outcome = {"status": "UNKNOWN", "error_code": "SEND_RESULT_UNKNOWN"}
            return {**outcome, "delivery_id": claimed["delivery_id"]}


def create_app(config=None, *, store=None, runtime=None, token=None, start_worker=True, collector=None, publisher=None, summarizer=None, clock=None):
    selected = profile_for(os.environ.get("BRE_NEWS_INDUSTRY", (config or {}).get("industry", "wind")))
    config = config or load_config(selected.env("CONFIG"), selected.industry)
    if config.get("industry", selected.industry) != selected.industry:
        raise ValueError("news_profile_mismatch")
    profile = bind_config(config, selected.industry)
    token = token if token is not None else profile.env("API_TOKEN")
    if len(token) < 32:
        raise ValueError("api_token_minimum_32_characters")
    runtime = Path(runtime or profile.env("RUNTIME_DIR")).resolve()
    repo = Path(__file__).resolve().parents[1]
    if store is None:
        if not profile.env("RUNTIME_DIR") and runtime == Path.cwd().resolve():
            raise ValueError("runtime_directory_required")
        if runtime == repo or repo in runtime.parents:
            raise ValueError("runtime_must_be_outside_repository")
        store = Store(runtime / (profile.industry + "-news.duckdb"))
    store.bind_industry(profile.industry)
    if profile.env("PUBLISH_CLONE"):
        config["policy"]["publisher"]["publish_clone"] = profile.env("PUBLISH_CLONE")
    if os.environ.get("BRE_NEWS_PUBLISH_LOCK"):
        config["policy"]["publisher"]["lock_path"] = os.environ["BRE_NEWS_PUBLISH_LOCK"]
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    if summarizer is None and config["policy"].get("summarizer", {}).get("provider") == "codex_oauth":
        from .codex_summary import CodexSummary
        summarizer = CodexSummary(config["policy"]["summarizer"])
    pipeline = Pipeline(store, config["collection"], config["policy"], summarizer)
    runner = JobRunner(store, pipeline, config, runtime, collector, publisher, clock)
    app.extensions.update(wind_news_store=store, wind_news_pipeline=pipeline, wind_news_runner=runner)

    def authenticated(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return jsonify(error_code="UNAUTHORIZED"), 401
            return fn(*args, **kwargs)
        return wrapped

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.errorhandler(Conflict)
    def conflict(exc):
        return jsonify(error_code=str(exc)), 409

    @app.errorhandler(ValueError)
    def invalid(exc):
        code = str(exc) if re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(exc)) else "INVALID_REQUEST"
        return jsonify(error_code=code), 400

    @app.errorhandler(TimeoutError)
    def db_busy(exc):
        return jsonify(error_code='NEWS_DB_TIMEOUT'), 503

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("json_object_required")
        return value

    def worker_failure():
        return runner.failure() if start_worker else None

    def enqueue(operation, payload, *, key=None):
        error = worker_failure()
        if error:
            return jsonify(error_code=error), 503
        key = key or request.headers.get('Idempotency-Key')
        schedule = scheduled_job(operation, key or '', payload, runner.clock())
        if schedule and operation == 'prepare':
            payload.setdefault('retry_blocked', True)
        job = store.submit(operation, key, payload, schedule=schedule, timeout=25)
        runner._wake.set()
        return jsonify(job_id=job["job_id"], status=job["status"]), 202

    @app.get('/health/live')
    @authenticated
    def live():
        error = worker_failure()
        if error:
            return jsonify(status='unavailable', error_code=error), 503
        return jsonify(status='live', industry=profile.industry)

    @app.get("/health/ready")
    @authenticated
    def health():
        error = worker_failure()
        if error:
            return jsonify(status='unavailable', error_code=error), 503
        store.call(lambda db: db.execute('SELECT 1').fetchone(), timeout=3)
        return jsonify(status="ready", schema_version=1, industry=profile.industry)

    @app.get("/v1/config/collection")
    @authenticated
    def collection_config():
        return jsonify(config["collection"])

    @app.post("/v1/collect")
    @authenticated
    def collect():
        return enqueue("collect", body())

    @app.post("/v1/ingest")
    @authenticated
    def ingest():
        return enqueue("ingest", body())

    @app.post("/v1/issues/prepare")
    @authenticated
    def prepare():
        data = body()
        window(data.get("issue_date", ""), data.get("cutoff"))
        return enqueue("prepare", data)

    @app.get("/v1/jobs/<job_id>")
    @authenticated
    def job(job_id):
        value = store.job(job_id)
        if not value:
            return jsonify(error_code="JOB_NOT_FOUND"), 404
        # No arbitrary upstream request body is exposed by polling.
        return jsonify({k: value[k] for k in ("job_id", "operation", "status", "result", "error_code", "created_at", "updated_at")})

    @app.get("/v1/issues")
    @authenticated
    def issues():
        day = request.args.get("issue_date")
        if day:
            return issue(profile.industry + "-" + day)
        return jsonify(issues=[present_issue(i) for i in store.list_issues()])

    @app.get("/v1/issues/<issue_id>")
    @authenticated
    def issue(issue_id):
        revision = request.args.get("revision", type=int)
        if "revision" in request.args and (revision is None or revision < 1):
            raise ValueError("invalid_revision")
        value = store.issue(issue_id, revision)
        if value is None:
            return jsonify(error_code="ISSUE_NOT_FOUND"), 404
        return jsonify(present_issue(value))

    @app.get("/v1/articles/<article_id>")
    @authenticated
    def article(article_id):
        value = store.call(lambda db: db.execute("SELECT metadata,evidence FROM articles WHERE article_id=?", [article_id]).fetchone())
        if not value:
            return jsonify(error_code="ARTICLE_NOT_FOUND"), 404
        return jsonify(article=json.loads(value[0]), evidence=json.loads(value[1]))

    @app.post("/v1/issues/<issue_id>/review")
    @authenticated
    def review(issue_id):
        return jsonify(present_issue(pipeline.review(issue_id, body())))

    @app.post("/v1/issues/<issue_id>/publish")
    @authenticated
    def publish(issue_id):
        data = body()
        data["issue_id"] = issue_id
        if not isinstance(data.get("revision"), int) or not isinstance(data.get("approval_hash"), str):
            raise ValueError("revision_and_approval_hash_required")
        return enqueue("publish", data)

    @app.post("/v1/backup")
    @authenticated
    def backup():
        body()
        return enqueue("backup", {})

    @app.post("/v1/reconcile")
    @authenticated
    def reconcile():
        body()
        return enqueue("reconcile", {})

    @app.get("/v1/deliveries/pending")
    @authenticated
    def pending():
        return jsonify(deliveries=store.pending_deliveries(request.args.get("channel_id", profile.channel), max_attempts=12))

    @app.post("/v1/deliveries/claim")
    @authenticated
    def claim():
        data = body()
        if not all(k in data for k in ("issue_id", "revision", "channel_id")):
            raise ValueError("delivery_key_required")
        if data["channel_id"] != profile.channel or not data["issue_id"].startswith(profile.industry + "-"):
            raise ValueError("delivery_channel_mismatch")
        return jsonify(store.claim_delivery(data["issue_id"], data["revision"], data["channel_id"], data.get("message_type", "daily")))

    @app.post("/v1/deliveries/<delivery_id>/result")
    @authenticated
    def result(delivery_id):
        data = body()
        evidence = {k: data[k] for k in ("message_id", "error_code") if k in data}
        if any(not isinstance(v, str) or len(v) > 150 or not re.fullmatch(r"[A-Za-z0-9_:.\-]+", v) for v in evidence.values()):
            raise ValueError("invalid_delivery_evidence")
        return jsonify(store.delivery_result(delivery_id, data.get("status"), evidence))

    @app.get("/v1/deliveries/status")
    @authenticated
    def delivery_status():
        now = runner.clock().astimezone(KST)
        day = request.args.get("issue_date") or now.date().isoformat()
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid_issue_date")
        record = store.verified_issue(profile.industry + "-" + day)
        channel = request.args.get("channel_id", profile.channel)
        prior = store.daily_delivery(day, channel)
        return jsonify(teams_configured=bool(profile.env("TEAMS_WEBHOOK_URL")),
                       card_format=profile.env("TEAMS_CARD_FORMAT", "adaptive"),
                       issue_date=day, revision=record["revision"] if record else None,
                       publication_state=record["state"] if record else None,
                       delivery_status=prior["status"] if prior else None,
                       delivery_id=prior["delivery_id"] if prior else None,
                       delivery_claimed_at=prior['claimed_at'] if prior else None,
                       delivery_evidence={k: (prior.get("result") or {})[k] for k in ("error_code", "http_status", "response_class", "receiver_error_code") if k in (prior.get("result") or {})} if prior else {},
                       briefing_stopped=bool(runner.briefing_stopped(day)) if channel == profile.channel else False,
                       schedule_owner="n8n", first_check="08:30", last_check="09:25", deadline="09:30", interval_minutes=5)

    @app.post("/v1/deliveries/preview")
    @authenticated
    def delivery_preview():
        data = body()
        issue_id = data.get("issue_id") or profile.industry + "-" + data.get("issue_date", runner.clock().astimezone(KST).date().isoformat())
        record = store.verified_issue(issue_id)
        if not record:
            return jsonify(error_code="WEB_VERIFICATION_REQUIRED"), 409
        from .delivery import build_card
        return jsonify(build_card(record["payload"], config["policy"].get("publisher", {}).get("pages_base_url", ""),
                                  profile.env("TEAMS_CARD_FORMAT", "adaptive"), industry=profile.industry))

    @app.post("/v1/deliveries/send")
    @authenticated
    def send():
        return jsonify(runner.send_delivery(body()))

    @app.post("/v1/briefings/check")
    @authenticated
    def briefing_check():
        data = body()
        now = runner.clock().astimezone(KST)
        day = now.date().isoformat()
        if set(data) - {"issue_date"} or data.get("issue_date", day) != day:
            raise ValueError("briefing_check_requires_today")
        if (now.hour, now.minute) < (8, 30):
            raise ValueError("OUTSIDE_DELIVERY_WINDOW")
        if (now.hour, now.minute) >= (9, 30):
            raise ValueError("BRIEFING_DELIVERY_DEADLINE_MISSED")
        # Slot identity survives n8n retries/restarts and duplicate triggers.
        return enqueue('briefing', {'issue_date': day}, key='morning-briefing-' + slot(now))

    @app.get('/v1/briefings/readiness')
    @authenticated
    def briefing_readiness():
        day = runner.clock().astimezone(KST).date().isoformat()
        failure = worker_failure()
        if failure:
            return jsonify(issue_date=day, stage=failure, web_verified=False), 503
        def read(db):
            issue_id = profile.issue_id(day)
            verified = db.execute("SELECT updated_at FROM issues WHERE issue_id=? AND state='WEB_VERIFIED' ORDER BY revision DESC LIMIT 1", [issue_id]).fetchone()
            current = db.execute("SELECT state FROM issues WHERE issue_id=? ORDER BY revision DESC LIMIT 1", [issue_id]).fetchone()
            job = db.execute("""SELECT operation,status FROM jobs JOIN job_schedule USING (job_id)
                WHERE schedule_date=? AND status IN ('RUNNING','QUEUED')
                AND schedule_group IN ('cutoff','prepare','publish','briefing')
                ORDER BY CASE status WHEN 'RUNNING' THEN 0 ELSE 1 END,created_at LIMIT 1""", [day]).fetchone()
            first = db.execute("""SELECT status,json_extract_string(result,'$.status') FROM jobs
                WHERE operation='briefing' AND idempotency_key=?""", ['morning-briefing-' + day + '-08-30']).fetchone()
            stage = 'WEB_VERIFIED' if verified else '_'.join(job).upper() if job else current[0] if current else 'AWAITING_CUTOFF_OR_PREPARATION'
            return {'issue_date': day, 'stage': stage, 'web_verified': bool(verified), 'verified_at': verified[0] if verified else None,
                    'first_slot_job': first[0] if first else None, 'first_slot_result': first[1] if first else None,
                    'auto_recover': bool(config['policy'].get('auto_recover')),
                    'collection_enabled': bool(config['collection'].get('enabled')),
                    'publisher_enabled': bool(config['policy']['publisher'].get('enabled'))}
        return jsonify(store.call(read, timeout=3))

    @app.get("/review")
    def review_ui():
        # The empty shell stores no data or token; all data requests are authenticated.
        return render_template("review.html", industry=profile.industry, label=profile.label, type_field=profile.type_field)

    if start_worker:
        runner.start()
    return app
