"""Bounded hourly collection cycle. No publication, notification, or service changes."""
import json
from datetime import timedelta, timezone
from .core import utc_now, instant
from .queue import SearchQueue
from .sources import enqueue_project_searches, run_job


def collection_cycle(store, configs, *, max_jobs=5, max_pages=10, fetcher=None, now=None):
    now = now or utc_now()
    today = instant(now).astimezone(timezone(timedelta(hours=9))).date().isoformat()
    queue = SearchQueue(store)
    generic = [key for key,c in configs.items() if c.get("window_mode") != "full_archive"]
    if generic:
        enqueue_project_searches(store,generic,today,incremental=True)
    for key,config in configs.items():
        for query in config.get("watch_queries",[]):
            queue.enqueue(key,query,"2001-01-01",today)
    # Keep successful pages in search_runs before re-checking overlapping windows.
    for job in queue.report():
        if job["source"] in configs and job["status"] == "done" and job["end"] == today:
            queue.refresh(job["id"],now=now)
    results=[]
    for _ in range(max_jobs):
        result=run_job(store,configs,max_pages=max_pages,fetcher=fetcher)
        results.append(result)
        if result["status"] == "idle":
            break
    # Discover detail/attachment jobs from completed pages. Titles remain candidates.
    for row in store.db.execute("SELECT q.source,p.data FROM queue q JOIN search_pages p ON q.id=p.job_id WHERE q.status='done'"):
        config=configs.get(row["source"])
        if not config or config.get("collect_attachments") is not True:
            continue
        payload=json.loads(row["data"])
        for item in payload.get("items",[])+payload.get("supplemental_items",[]):
            if not item.get("url") or not any(term in item.get("title","") for term in config.get("watch_queries",[])):
                continue
            queue.enqueue(row["source"],item["url"],today,today,metadata={"kind":"notice_detail","title":item["title"]})
    return {"status":"blocked" if any(r["status"]=="blocked" for r in results) else "processed",
            "jobs":results,"publication":"not_requested"}