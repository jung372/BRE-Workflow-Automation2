"""Durable, leased search windows with atomic page checkpoints and overlap retries."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import uuid
from .core import WindError, canonical, day, instant, stable_id, utc_now

class SearchQueue:
    def __init__(self, store):
        self.store = store

    def enqueue(self, source, query, start, end, *, metadata=None):
        if not source or not query or day(start) > day(end):
            raise WindError("Invalid search interval")
        ident = stable_id("job", [source, query, start, end])
        with self.store.transaction():
            self.store.db.execute("INSERT OR IGNORE INTO queue(id,source,query,start,end,data) VALUES(?,?,?,?,?,?)",
                                  (ident, source, query, start, end, canonical(metadata or {}).decode()))
        return ident

    def backfill(self, source, query, start, end, *, metadata=None):
        start, end = day(start), day(end)
        result = []
        while start <= end:
            stop = min(date(start.year, 12, 31), end)
            result.append(self.enqueue(source, query, start.isoformat(), stop.isoformat(), metadata=metadata))
            start = stop + timedelta(days=1)
        return result

    def incremental(self, source, query, end, *, overlap_days=14, earliest):
        if not 1 <= overlap_days <= 365:
            raise WindError("Invalid overlap")
        row = self.store.db.execute("SELECT MAX(end) FROM queue WHERE source=? AND query=? AND (status='done' OR json_extract(data,'$.last_completed_at') IS NOT NULL)", (source, query)).fetchone()
        start = max(day(earliest), day(row[0]) - timedelta(days=overlap_days)) if row[0] else day(earliest)
        return self.backfill(source, query, start.isoformat(), end)

    def claim(self, *, now=None, lease_seconds=120, source_ids=None):
        now = instant(now or utc_now()).isoformat()
        expiry = (instant(now) + timedelta(seconds=lease_seconds)).isoformat()
        with self.store.transaction():
            condition = "(status='pending' OR (status='running' AND lease_until<?))"
            values = [now]
            if source_ids is not None:
                source_ids = list(source_ids)
                if not source_ids:
                    return None
                condition += " AND source IN (" + ",".join("?" for _ in source_ids) + ")"
                values.extend(source_ids)
            row = self.store.db.execute("SELECT * FROM queue WHERE " + condition + " ORDER BY CASE WHEN json_extract(data,'$.kind')='notice_detail' THEN 0 ELSE 1 END,start,id LIMIT 1", values).fetchone()
            if not row:
                return None
            value = dict(row)
            data = json.loads(value["data"]); data["lease_token"] = uuid.uuid4().hex
            self.store.db.execute("UPDATE queue SET status='running',lease_until=?,attempts=attempts+1,data=? WHERE id=?",
                                  (expiry, canonical(data).decode(), value["id"]))
            return {**value, "data": data, "lease_until": expiry, "status": "running"}

    def _owned(self, job):
        row = self.store.db.execute("SELECT * FROM queue WHERE id=?", (job["id"],)).fetchone()
        if not row or row["status"] != "running" or json.loads(row["data"]).get("lease_token") != job["data"]["lease_token"]:
            raise WindError("Search lease is no longer owned")
        return row

    def checkpoint(self, job, page, *, now=None):
        """page: items with stable IDs, total (optional), last_page, complete_proof.
        Transport errors, login shells, and unknown empty pages never mean zero results.
        """
        rows = page.get("items")
        if not isinstance(rows, list) or any(not r.get("id") for r in rows):
            raise WindError("Invalid source records")
        if len({r["id"] for r in rows}) != len(rows):
            raise WindError("Duplicate IDs within search page")
        fingerprint = hashlib.sha256(canonical(sorted(r["id"] for r in rows))).hexdigest()
        with self.store.transaction():
            current = self._owned(job)
            cursor = current["cursor"]
            previous = list(self.store.db.execute("SELECT data,fingerprint FROM search_pages WHERE job_id=?", (job["id"],)))
            if rows and any(r["fingerprint"] == fingerprint for r in previous):
                raise WindError("Pagination repeated a page; split the date interval")
            prior_ids = {item["id"] for r in previous for item in json.loads(r["data"])["items"]}
            if prior_ids & {r["id"] for r in rows}:
                raise WindError("Pagination overlaps; retry a stable interval")
            total = page.get("total")
            if total is not None and (type(total) is not int or total < len(prior_ids) + len(rows)):
                raise WindError("Invalid source result count")
            prior_totals = {json.loads(r["data"]).get("total") for r in previous} - {None}
            if total is not None and prior_totals and prior_totals != {total}:
                raise WindError("Source changed during pagination")
            last = page.get("last_page") is True
            if last and (not page.get("complete_proof") or (total is not None and len(prior_ids) + len(rows) != total)):
                raise WindError("Unproven or truncated end of results")
            if not rows and not last:
                raise WindError("Empty page without verified result boundary")
            self.store.db.execute("INSERT INTO search_pages VALUES (?,?,?,?)", (job["id"], cursor, fingerprint, canonical(page).decode()))
            data = json.loads(current["data"])
            if last:
                data["completed_at"] = now or utc_now()
            expiry = (instant(now or utc_now()) + timedelta(seconds=120)).isoformat()
            self.store.db.execute("UPDATE queue SET cursor=?,status=?,lease_until=?,error=NULL,data=? WHERE id=?",
                                  (cursor + 1, "done" if last else "running", expiry, canonical(data).decode(), job["id"]))
        job["cursor"] = cursor + 1
        return last

    def fail(self, job, message):
        with self.store.transaction():
            self._owned(job)
            # Store a reason code, not arbitrary HTTP content which could contain credentials.
            self.store.db.execute("UPDATE queue SET status='blocked',error=?,lease_until=NULL WHERE id=?",
                                  (message[:160], job["id"]))

    def retry(self, ident):
        with self.store.transaction():
            changed = self.store.db.execute("UPDATE queue SET status='pending',error=NULL WHERE id=? AND status='blocked'", (ident,)).rowcount
            if not changed:
                raise WindError("Only blocked jobs can be retried")

    def split(self, ident):
        row = self.store.db.execute("SELECT * FROM queue WHERE id=?", (ident,)).fetchone()
        if not row or row["status"] != "blocked":
            raise WindError("Split requires a blocked interval")
        start, end = day(row["start"]), day(row["end"])
        if start == end:
            raise WindError("One-day interval cannot split; manual source review required")
        middle = start + (end - start) // 2
        meta = json.loads(row["data"]); meta.pop("lease_token", None)
        ids = [self.enqueue(row["source"], row["query"], start.isoformat(), middle.isoformat(), metadata=meta),
               self.enqueue(row["source"], row["query"], (middle + timedelta(days=1)).isoformat(), end.isoformat(), metadata=meta)]
        with self.store.transaction():
            self.store.db.execute("UPDATE queue SET status='split' WHERE id=?", (ident,))
        return ids

    def refresh(self, ident, *, now=None, minimum_seconds=3600):
        now = instant(now or utc_now())
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM queue WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "done":
                return False
            data = json.loads(row["data"])
            completed = instant(data["completed_at"])
            if (now - completed).total_seconds() < minimum_seconds:
                return False
            pages = [dict(p) for p in self.store.db.execute("SELECT * FROM search_pages WHERE job_id=? ORDER BY cursor", (ident,))]
            run_id = stable_id("run", [ident, data["completed_at"], data.get("lease_token")])
            self.store.db.execute("INSERT OR IGNORE INTO search_runs VALUES (?,?,?,?)",
                (run_id, ident, data["completed_at"], canonical({"job":dict(row), "pages":pages}).decode()))
            data["last_completed_at"] = data.pop("completed_at")
            data.pop("lease_token", None)
            self.store.db.execute("DELETE FROM search_pages WHERE job_id=?", (ident,))
            self.store.db.execute("UPDATE queue SET status='pending',cursor=1,lease_until=NULL,data=? WHERE id=?",
                (canonical(data).decode(),ident))
        return True

    def report(self):
        return [dict(r) for r in self.store.db.execute("SELECT id,source,query,start,end,cursor,status,attempts,error FROM queue ORDER BY start,id")]