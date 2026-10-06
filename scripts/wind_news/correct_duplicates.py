"""Correct today's duplicates using approved members and their frozen batches.

Run inside the news container. Never recollect, open the live DB, edit old issue
files, override held summaries, or send Teams. All writes use the service API.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

import duckdb
import requests

from news.dedup import RULE_VERSION, group_events
from news.pipeline import KST, classify, event_key

REASON = "동일 업무협약 보도를 당사자·체결일·협약 목적 기준으로 통합; 기존 승인 기사와 수집 배치로 정정"


def check(condition, code):
    if not condition:
        raise ValueError(code)


def published_members(record):
    selected = {x["event_id"] for x in record["issue"]["items"]}
    members = set()
    for candidate in record["candidates"]:
        if candidate["item"]["event_id"] in selected:
            check(candidate["approved"] and not candidate["held"], "ORIGINAL_SELECTION_NOT_APPROVED")
            members.update(candidate["member_article_ids"])
    check(members and selected, "ORIGINAL_SELECTION_EMPTY")
    check(all(x["representative_article_id"] in members for x in record["issue"]["items"]), "ORIGINAL_MEMBERS_MISSING")
    return sorted(members)


def frozen_members(backup, batch_ids, selected_ids):
    check(batch_ids, "FROZEN_BATCHES_MISSING")
    with duckdb.connect(str(backup), read_only=True) as db:
        marks = ",".join("?" for _ in batch_ids)
        rows = db.execute("SELECT b.article_snapshot FROM batch_articles b JOIN batches z USING(batch_id) WHERE b.batch_id IN (" + marks + ") ORDER BY z.created_at,z.batch_id", batch_ids).fetchall()
    selected, articles = set(selected_ids), {}
    for row in rows:
        check(row[0], "FROZEN_SNAPSHOT_MISSING")
        article = json.loads(row[0])
        if article["article_id"] in selected:
            check(article.get("text") or article.get("description"), "FROZEN_EVIDENCE_MISSING")
            articles[article["article_id"]] = article
    check(set(articles) == selected, "FROZEN_MEMBERS_MISSING")
    for article in articles.values():
        article.update(classify(article))
        article["event_key"] = event_key(article, article)
        article["event_id"] = "evt-" + article["event_key"][:24]
    return list(articles.values())


def main():
    session = requests.Session()
    session.trust_env = False
    session.headers["Authorization"] = "Bearer " + os.environ["WIND_NEWS_API_TOKEN"]
    base = "http://127.0.0.1:8090"
    def get(path):
        # The backup owns the serialized DB connection while copying. Polling
        # may wait behind that transaction even though the HTTP worker is healthy.
        response = session.get(base + path, timeout=60)
        response.raise_for_status()
        return response.json()
    def job(path, payload, key):
        response = session.post(base + path, json=payload, headers={"Idempotency-Key": key}, timeout=20)
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            record = get("/v1/jobs/" + job_id)
            if record["status"] == "SUCCEEDED":
                return record["result"]
            check(record["status"] != "FAILED", record.get("error_code") or "CORRECTION_JOB_FAILED")
            time.sleep(5)
        raise ValueError("CORRECTION_JOB_TIMEOUT")

    check(get("/health/ready")["status"] == "ready", "NEWS_NOT_READY")
    issue_id = "wind-" + datetime.now(KST).date().isoformat()
    original = get("/v1/issues/" + issue_id)
    resume = original["issue"].get("correction_reason") == REASON
    if resume and original["state"] == "WEB_VERIFIED":
        print("DEDUP_CORRECTION_ALREADY_VERIFIED=" + issue_id, flush=True)
        return
    check(original["state"] in ({"READY", "COMMITTED", "WEB_VERIFIED"} if resume else {"COMMITTED", "WEB_VERIFIED"}), "TODAY_ISSUE_NOT_PUBLISHED")
    selected_ids = published_members(original)
    key = f"dedup-{issue_id}-r{original['revision']}-{RULE_VERSION}"
    backup_result = job("/v1/backup", {}, key + "-backup")
    backup = Path(os.environ["WIND_NEWS_RUNTIME_DIR"]) / "backups" / backup_result["filename"]
    check(hashlib.sha256(backup.read_bytes()).hexdigest() == backup_result["sha256"], "BACKUP_HASH_MISMATCH")
    print("DEDUP_BACKUP_HASH=" + backup_result["sha256"], flush=True)
    groups = group_events(frozen_members(backup, original["batch_ids"], selected_ids))
    expected_topics = len(groups)
    print("DEDUP_PREFLIGHT=" + json.dumps({"issue_id": issue_id, "revision": original["revision"], "approved_articles": len(selected_ids), "old_topics": len(original["issue"]["items"]), "new_topics": expected_topics}), flush=True)
    if resume:
        issue = original
    else:
        check(expected_topics < len(original["issue"]["items"]), "NO_DUPLICATE_REDUCTION")
        issue = job("/v1/issues/prepare", {"issue_date": original["issue"]["issue_date"],
            "batch_ids": original["batch_ids"], "article_ids": selected_ids,
            "correction_reason": REASON}, key + "-prepare")
    check(issue["state"] in {"READY", "COMMITTED", "WEB_VERIFIED"}, "CORRECTION_NOT_READY")
    check(issue["issue"]["counts"]["held"] == 0 and issue["issue"]["counts"].get("deferred", 0) == 0, "CORRECTION_HAS_UNVERIFIED_TOPICS")
    check(len(issue["issue"]["items"]) == expected_topics, "CORRECTION_TOPIC_COUNT_MISMATCH")
    corrected_ids = {x for c in issue["candidates"] for x in c["member_article_ids"]}
    check(corrected_ids == set(selected_ids), "CORRECTION_MEMBER_SELECTION_CHANGED")
    selected_events = {x["event_id"] for x in issue["issue"]["items"]}
    check(selected_events == {c["item"]["event_id"] for c in issue["candidates"] if c["approved"] and not c["held"]}, "CORRECTION_SELECTION_NOT_VERIFIED")
    published = issue
    for attempt in range(4):
        if published["state"] == "WEB_VERIFIED":
            break
        published = job("/v1/issues/" + issue_id + "/publish", {"revision": issue["revision"],
            "approval_hash": issue["approval_hash"]}, f"dedup-publish-{issue_id}-r{issue['revision']}-{attempt}")
        if published["state"] == "COMMITTED":
            time.sleep(20)
    check(published["state"] == "WEB_VERIFIED", "CORRECTION_WEB_VERIFICATION_PENDING")
    print("DEDUP_CORRECTION_RESULT=" + json.dumps({"issue_id": issue_id, "revision": published["revision"], "state": published["state"], "counts": published["issue"]["counts"], "content_hash": published["content_hash"]}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"[A-Z0-9_]{1,100}", str(error)) else "CORRECTION_OPERATOR_FAILED"
        print("DEDUP_CORRECTION_ERROR=" + code, flush=True)
        raise SystemExit(1)
