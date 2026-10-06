"""Authenticated internal API. Persistent jobs precede 202 responses."""
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import re
import threading

from flask import Flask, jsonify, render_template, request

from .pipeline import KST, Pipeline, approval_digest, window
from .store import Conflict, Store, canonical, utcnow


def load_config(path=None):
    if path:
        with Path(path).open(encoding="utf-8") as stream:
            config = json.load(stream)
        if "collection" not in config or "policy" not in config:
            raise ValueError("config_requires_collection_and_policy")
        return config
    root = Path(__file__).resolve().parents[1] / "config"
    return {"collection": json.loads((root / "wind_news_collection.json").read_text(encoding="utf-8")), "policy": json.loads((root / "wind_news_policy.json").read_text(encoding="utf-8"))}


def present_issue(record):
    if record is None:
        return None
    result = dict(record)
    result["issue"] = result.pop("payload")
    return result


class JobRunner:
    def __init__(self, store, pipeline, config, runtime, collector=None, publisher=None, clock=None):
        self.store, self.pipeline, self.config = store, pipeline, config
        self.runtime = Path(runtime)
        self.collector, self.publisher = collector, publisher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        self.store.recover()
        self._thread = threading.Thread(target=self._loop, name="wind-news-jobs", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=35)

    def _loop(self):
        while not self._stop.is_set():
            if not self.run_once():
                self._wake.wait(1)
                self._wake.clear()

    def run_once(self):
        job = self.store.next_job()
        if not job:
            return False
        try:
            result = self.execute(job)
            self.store.finish_job(job["job_id"], result=result)
        except (Conflict, ValueError) as exc:
            code = str(exc) if re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(exc)) else "VALIDATION_FAILED"
            self.store.finish_job(job["job_id"], error_code=code)
        except Exception:
            # No exception/URL/header text is copied to the API or persistent job.
            self.store.finish_job(job["job_id"], error_code="INTERNAL_JOB_ERROR")
        return True

    def execute(self, job):
        operation, payload = job["operation"], job["payload"]
        if operation == "ingest":
            return self.pipeline.ingest(payload, job["job_id"])
        if operation == "collect":
            from .collector import Collector
            collector = self.collector or Collector(self.config["collection"])
            until = self.clock()
            since = until - timedelta(hours=max(72, self.config["collection"].get("overlap_hours", 72)))
            batch = collector.collect(since=since, until=until)
            return self.pipeline.ingest(batch, job["job_id"])
        if operation == "prepare":
            return present_issue(self.pipeline.prepare(payload))
        if operation == "publish":
            return self.publish(payload)
        if operation == "backup":
            self.pipeline.purge_expired_evidence()
            destination = self.runtime / "backups" / ("wind-news-" + job["job_id"] + ".duckdb")
            if destination.exists():
                import hashlib
                return {"filename": destination.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
            return self.store.backup(destination)
        if operation == "reconcile":
            return self.reconcile()
        raise ValueError("unsupported_job_operation")

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
            schema = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "wind-news-issue.json").read_text(encoding="utf-8"))
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
        if policy.get("auto_recover", False) and self.config["collection"].get("enabled", False):
            day = now.date().isoformat()
            current = self.store.issue("wind-" + day)
            if (not current or current["state"] == "BLOCKED") and (now.hour, now.minute) >= (7, 40):
                latest_batch = self.store.call(lambda db: db.execute("SELECT batch_id FROM batches ORDER BY created_at DESC,batch_id DESC LIMIT 1").fetchone())
                batch_suffix = latest_batch[0] if latest_batch else "none"
                job = self.store.submit("prepare", "recovery-prepare-" + day + "-" + batch_suffix, {"issue_date": day, "retry_blocked": True})
                enqueued.append(job["job_id"])
            elif current and current["state"] in {"READY", "COMMITTED"} and (now.hour, now.minute) >= (7, 50) and policy.get("publisher", {}).get("enabled"):
                previous_attempts = self.store.call(lambda db: db.execute("SELECT status FROM jobs WHERE operation='publish' AND idempotency_key LIKE ? ORDER BY created_at", ["recovery-publish-" + day + "-" + str(current["revision"]) + "-%"]).fetchall())
                if not any(row[0] in {"QUEUED", "RUNNING"} for row in previous_attempts) and len(previous_attempts) < 12:
                    job = self.store.submit("publish", "recovery-publish-" + day + "-" + str(current["revision"]) + "-" + str(len(previous_attempts) + 1), {"issue_id": current["issue_id"], "revision": current["revision"], "approval_hash": current["approval_hash"]})
                    enqueued.append(job["job_id"])
        return {"enqueued_jobs": enqueued, "pending_deliveries": self.store.pending_deliveries(), "pending_web_verifications": [{"issue_id": i["issue_id"], "revision": i["revision"]} for i in self.store.list_issues() if i["state"] == "COMMITTED"], "unknown_deliveries": self.store.call(lambda db: self.store._deliveries(db, "status='UNKNOWN'")), "expired_evidence_cleaned": self.pipeline.purge_expired_evidence()}


def create_app(config=None, *, store=None, runtime=None, token=None, start_worker=True, collector=None, publisher=None, summarizer=None, clock=None):
    config = config or load_config(os.environ.get("WIND_NEWS_CONFIG"))
    token = token if token is not None else os.environ.get("WIND_NEWS_API_TOKEN", "")
    if len(token) < 32:
        raise ValueError("api_token_minimum_32_characters")
    runtime = Path(runtime or os.environ.get("WIND_NEWS_RUNTIME_DIR", "")).resolve()
    repo = Path(__file__).resolve().parents[1]
    if store is None:
        if not os.environ.get("WIND_NEWS_RUNTIME_DIR") and runtime == Path.cwd().resolve():
            raise ValueError("runtime_directory_required")
        if runtime == repo or repo in runtime.parents:
            raise ValueError("runtime_must_be_outside_repository")
        store = Store(runtime / "wind-news.duckdb")
    if os.environ.get("WIND_NEWS_PUBLISH_CLONE"):
        config["policy"].setdefault("publisher", {})["publish_clone"] = os.environ["WIND_NEWS_PUBLISH_CLONE"]
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    if summarizer is None and config["policy"].get("summarizer", {}).get("provider") == "codex_oauth":
        from .codex_summary import CodexSummary
        summarizer = CodexSummary(config["policy"]["summarizer"])
    pipeline = Pipeline(store, config["collection"], config["policy"], summarizer)
    runner = JobRunner(store, pipeline, config, runtime, collector, publisher, clock)
    app.extensions.update(wind_news_store=store, wind_news_pipeline=pipeline, wind_news_runner=runner)
    delivery_lock = threading.Lock()

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

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("json_object_required")
        return value

    def enqueue(operation, payload):
        job = store.submit(operation, request.headers.get("Idempotency-Key"), payload)
        runner._wake.set()
        return jsonify(job_id=job["job_id"], status=job["status"]), 202

    @app.get("/health/ready")
    @authenticated
    def health():
        store.call(lambda db: db.execute("SELECT 1").fetchone())
        return jsonify(status="ready", schema_version=1)

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
            return issue("wind-" + day)
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
        return jsonify(deliveries=store.pending_deliveries(request.args.get("channel_id", "wind-news")))

    @app.post("/v1/deliveries/claim")
    @authenticated
    def claim():
        data = body()
        if not all(k in data for k in ("issue_id", "revision", "channel_id")):
            raise ValueError("delivery_key_required")
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
        day = now.date().isoformat()
        record = store.verified_issue("wind-" + day)
        channel = request.args.get("channel_id", "wind-news")
        prior = store.daily_delivery(day, channel)
        return jsonify(teams_configured=bool(os.environ.get("WIND_NEWS_TEAMS_WEBHOOK_URL")),
                       issue_date=day, revision=record["revision"] if record else None,
                       publication_state=record["state"] if record else None,
                       delivery_status=prior["status"] if prior else None)

    @app.post("/v1/deliveries/preview")
    @authenticated
    def delivery_preview():
        data = body()
        issue_id = data.get("issue_id") or "wind-" + data.get("issue_date", runner.clock().astimezone(KST).date().isoformat())
        record = store.verified_issue(issue_id)
        if not record:
            return jsonify(error_code="WEB_VERIFICATION_REQUIRED"), 409
        from .delivery import build_card
        return jsonify(build_card(record["payload"], config["policy"].get("publisher", {}).get("pages_base_url", "")))

    @app.post("/v1/deliveries/send")
    @authenticated
    def send():
        data = body()
        now = runner.clock().astimezone(KST)
        scheduled = data.get("scheduled", False)
        if not isinstance(scheduled, bool):
            raise ValueError("scheduled_must_be_boolean")
        kind = data.get("message_type", "daily")
        issue_id = data.get("issue_id") or "wind-" + data.get("issue_date", now.date().isoformat())
        if scheduled:
            if issue_id != "wind-" + now.date().isoformat() or kind != "daily" or "revision" in data:
                raise ValueError("scheduled_delivery_requires_today_latest_daily")
            if not 480 <= now.hour * 60 + now.minute <= 540:
                return jsonify(status="SKIPPED", error_code="OUTSIDE_DELIVERY_WINDOW")
        record = (store.verified_issue(issue_id) if data.get("revision") is None
                  else store.issue(issue_id, data["revision"]))
        if not record or record["state"] != "WEB_VERIFIED":
            code = "PUBLICATION_NOT_READY_BY_DEADLINE" if scheduled and now.hour == 9 else "WEB_VERIFICATION_REQUIRED"
            return jsonify(status="SKIPPED", error_code=code)
        webhook = os.environ.get("WIND_NEWS_TEAMS_WEBHOOK_URL", "")
        if not webhook:
            return jsonify(status="SKIPPED", error_code="TEAMS_NOT_CONFIGURED")
        with delivery_lock:
            claimed = store.claim_delivery(issue_id, record["revision"], data.get("channel_id", "wind-news"), kind)
            if not claimed["claimed"]:
                return jsonify(status=claimed["status"], delivery_id=claimed["delivery_id"], claimed=False)
            from .delivery import send_card
            try:
                outcome = send_card(record["payload"], webhook, config["policy"].get("publisher", {}).get("pages_base_url", ""))
                state = outcome.get("status", "UNKNOWN")
                if state == "SKIPPED":
                    state = "FAILED"
                store.delivery_result(claimed["delivery_id"], state, {"error_code": outcome.get("error_code", "")})
            except Exception:
                store.delivery_result(claimed["delivery_id"], "UNKNOWN", {"error_code": "SEND_RESULT_UNKNOWN"})
                outcome = {"status": "UNKNOWN", "error_code": "SEND_RESULT_UNKNOWN"}
            return jsonify(**outcome, delivery_id=claimed["delivery_id"])

    @app.get("/review")
    def review_ui():
        # The empty shell stores no data or token; all data requests are authenticated.
        return render_template("review.html")

    if start_worker:
        runner.start()
    return app
