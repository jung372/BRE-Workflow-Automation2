"""Inspect a service-managed snapshot; output only dates, counts and safe codes."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

import duckdb
import requests


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,100}", value) else None


def main():
    day = os.environ.get("NEWS_REPORT_DATE") or datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    date.fromisoformat(day)
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer " + os.environ["WIND_NEWS_API_TOKEN"]
    base = "http://127.0.0.1:8090"
    response = session.get(base + "/v1/deliveries/status", timeout=60)
    response.raise_for_status()
    status = response.json()
    print("BRIEFING_CURRENT=" + json.dumps({k: status.get(k) for k in (
        "issue_date", "revision", "teams_configured", "card_format", "publication_state", "delivery_status",
        "first_check", "last_check", "interval_minutes", "schedule_owner")}))
    response = session.post(base + "/v1/backup", json={},
                            headers={"Idempotency-Key": "briefing-diagnose-" + uuid.uuid4().hex}, timeout=60)
    response.raise_for_status()
    job_id = response.json()["job_id"]
    for _ in range(90):
        response = session.get(base + "/v1/jobs/" + job_id, timeout=60)
        response.raise_for_status()
        job = response.json()
        if job["status"] not in {"QUEUED", "RUNNING"}:
            break
        time.sleep(1)
    if job["status"] != "SUCCEEDED":
        raise ValueError("DIAGNOSTIC_BACKUP_NOT_READY")
    snapshot = Path(os.environ["WIND_NEWS_RUNTIME_DIR"]) / "backups" / job["result"]["filename"]
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != job["result"]["sha256"]:
        raise ValueError("DIAGNOSTIC_BACKUP_HASH_MISMATCH")
    start = datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=9))).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    with duckdb.connect(str(snapshot), read_only=True) as db:
        rows = db.execute("SELECT operation,status,error_code,created_at,updated_at,result FROM jobs "
                          "WHERE created_at>=? AND created_at<? AND operation IN ('collect','prepare','publish','briefing') "
                          "ORDER BY created_at LIMIT 100", [start.isoformat(), end.isoformat()]).fetchall()
        for op, state, error, created, updated, result in rows:
            result = json.loads(result) if result else {}
            print("BRIEFING_JOB=" + json.dumps({"operation": op, "status": state, "error_code": safe_code(error),
                "created_at": created, "updated_at": updated,
                "result": {k: safe_code(result.get(k)) for k in ("state", "status", "error_code") if k in result}}))
        for revision, state, created, updated, payload, candidates in db.execute(
                "SELECT revision,state,created_at,updated_at,payload,candidates FROM issues WHERE issue_date=? ORDER BY revision", [day]).fetchall():
            payload = json.loads(payload)
            reasons = Counter(safe_code(c.get("reason")) or "OTHER" for c in json.loads(candidates))
            print("BRIEFING_ISSUE=" + json.dumps({"revision": revision, "state": state, "created_at": created,
                "updated_at": updated, "published_at": payload.get("published_at"), "counts": payload.get("counts"), "reasons": reasons}))
        for state, created, updated, result in db.execute(
                "SELECT status,claimed_at,updated_at,result FROM deliveries WHERE issue_id=? ORDER BY claimed_at", ["wind-" + day]).fetchall():
            result = json.loads(result) if result else {}
            print("BRIEFING_SEND=" + json.dumps({"status": state, "claimed_at": created, "updated_at": updated,
                "http_status": result.get("http_status"), "error_code": safe_code(result.get("error_code"))}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("BRIEFING_DIAGNOSTIC_FAILED")
