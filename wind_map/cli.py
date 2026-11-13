"""Offline-first administrative commands. Live collection and publication are explicit."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import sqlite3
from .core import WindError, canonical, utc_now
from .extract import extract_pdf, hwpx_text, import_page_review
from .queue import SearchQueue
from .snapshot import build_snapshot, publication_gates, write_snapshot, review_digest
from .store import Store


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def parser():
    p = argparse.ArgumentParser(description="풍력대동여지도 비공개 저장·검토·정적 발행 도구")
    p.add_argument("--runtime", help="Private runtime directory (or BRE_WIND_MAP_RUNTIME)")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    for name in ("register", "import"):
        a = commands.add_parser(name); a.add_argument("file")
        if name == "register":
            a.add_argument("--metadata", required=True)
        else:
            a.add_argument("--actor", required=True); a.add_argument("--reason", required=True)
    a = commands.add_parser("document-metadata")
    a.add_argument("document_id"); a.add_argument("file"); a.add_argument("--actor", required=True); a.add_argument("--reason", required=True)
    a = commands.add_parser("extract")
    a.add_argument("document_id"); a.add_argument("--pages", help="Explicit comma-separated 1-based pages; omission means all")
    a = commands.add_parser("ocr-page")
    a.add_argument("document_id"); a.add_argument("page", type=int); a.add_argument("--executable", default="tesseract"); a.add_argument("--language", default="kor+eng")
    a = commands.add_parser("reopen-page")
    a.add_argument("document_id"); a.add_argument("page", type=int); a.add_argument("--actor", required=True); a.add_argument("--reason", required=True)
    a = commands.add_parser("review-page")
    a.add_argument("document_id"); a.add_argument("page", type=int); a.add_argument("file")
    a.add_argument("--actor", required=True); a.add_argument("--reason", required=True)
    a = commands.add_parser("review")
    a.add_argument("table"); a.add_argument("id"); a.add_argument("state", choices=["approved", "rejected", "pending"])
    a.add_argument("--actor", required=True); a.add_argument("--reason", required=True)
    a = commands.add_parser("export-review")
    a.add_argument("output")
    a = commands.add_parser("enqueue")
    a.add_argument("source"); a.add_argument("query"); a.add_argument("--start", required=True); a.add_argument("--end", required=True)
    a.add_argument("--incremental", action="store_true"); a.add_argument("--single-window", action="store_true")
    a = commands.add_parser("enqueue-projects")
    a.add_argument("--sources", required=True); a.add_argument("--end", required=True); a.add_argument("--incremental", action="store_true")
    a = commands.add_parser("collect-notice")
    a.add_argument("url"); a.add_argument("--agency", required=True); a.add_argument("--hosts", required=True); a.add_argument("--quarter"); a.add_argument("--title")
    a.add_argument("--live", action="store_true", required=True)
    for name in ("retry", "split", "refresh-search"):
        a = commands.add_parser(name); a.add_argument("id")
    a = commands.add_parser("cycle")
    a.add_argument("--sources", required=True); a.add_argument("--live", action="store_true", required=True)
    a.add_argument("--max-jobs", type=int, default=5); a.add_argument("--max-pages", type=int, default=10)
    a = commands.add_parser("collect")
    a.add_argument("--sources", required=True); a.add_argument("--live", action="store_true", required=True)
    a.add_argument("--max-pages", type=int, default=10); a.add_argument("--portal", action="store_true")
    for name in ("build", "publish"):
        a = commands.add_parser(name); a.add_argument("--as-of", required=True); a.add_argument("--known-at", default=None)
        if name == "build":
            a.add_argument("--output", required=True); a.add_argument("--preview", action="store_true")
        else:
            a.add_argument("--clone", required=True); a.add_argument("--remote", required=True)
            a.add_argument("--branch", default="main"); a.add_argument("--confirm-push", action="store_true", required=True)
    a = commands.add_parser("backup"); a.add_argument("output")
    a = commands.add_parser("backup-bundle"); a.add_argument("output")
    a = commands.add_parser("restore-bundle"); a.add_argument("source"); a.add_argument("destination")
    a = commands.add_parser("validate-public"); a.add_argument("root"); a.add_argument("--allow-preview", action="store_true")
    a = commands.add_parser("rollback-pointer"); a.add_argument("root")
    a = commands.add_parser("verify-public-url"); a.add_argument("url"); a.add_argument("--expected-snapshot")
    return p


def execute(args):
    if args.command == "verify-public-url":
        from .publication import verify_remote
        return verify_remote(args.url, expected_snapshot=args.expected_snapshot)
    if args.command == "restore-bundle":
        from .backup import restore_bundle
        return restore_bundle(args.source, args.destination)
    if args.command in {"validate-public", "rollback-pointer"}:
        from .publication import validate_tree, rollback
        return validate_tree(args.root, allow_preview=args.allow_preview) if args.command == "validate-public" else rollback(args.root)
    runtime = args.runtime or os.getenv("BRE_WIND_MAP_RUNTIME")
    if not runtime:
        base = os.getenv("BRE_RUNTIME_DIR")
        runtime = str(Path(base) / "wind-map") if base else None
    if not runtime:
        raise WindError("Explicit private --runtime or BRE_WIND_MAP_RUNTIME is required")
    with Store(runtime) as store:
        queue = SearchQueue(store)
        if args.command == "init":
            return {"database": str(store.path), "schema_version": 1}
        if args.command == "status":
            return {"counts": {t: len(store.all(t)) for t in ("documents", "projects", "permits", "events")},
                    "publication_blocks": publication_gates(store), "jobs": queue.report(),
                    "pages": [dict(r) for r in store.db.execute("SELECT status,reviewed,COUNT(*) AS count FROM pages GROUP BY status,reviewed")]}
        if args.command == "register":
            meta = read_json(args.metadata)
            content = Path(args.file).read_bytes()
            if meta["format"] == "pdf":
                import pdfplumber, io
                with pdfplumber.open(io.BytesIO(content)) as pdf:
                    meta["page_count"] = len(pdf.pages)
            return {"document_id": store.register_document(content, meta)}
        if args.command == "import":
            return {"changed": store.import_bundle(read_json(args.file), actor=args.actor, reason=args.reason)}
        if args.command == "document-metadata":
            return store.amend_document_metadata(args.document_id, read_json(args.file), actor=args.actor, reason=args.reason)
        if args.command == "extract":
            doc = store.document(args.document_id)
            if doc["format"] == "hwpx":
                text = hwpx_text((store.root / doc["asset"]).read_bytes())
                store.record_page(doc["id"], 1, [{"classification": "review", "text": text}], status="needs_review", metadata={"extractor_version": "hwpx-text-1"})
                return {"status": "needs_review"}
            if doc["format"] != "pdf":
                raise WindError("Binary HWP requires an externally converted PDF and linked source provenance")
            pages = [int(v) for v in args.pages.split(",")] if args.pages else None
            return extract_pdf(store, args.document_id, page_numbers=pages)
        if args.command == "ocr-page":
            from .extract import ocr_page
            return ocr_page(store,args.document_id,args.page,executable=args.executable,language=args.language)
        if args.command == "reopen-page":
            store.reopen_page(args.document_id,args.page,actor=args.actor,reason=args.reason)
            return {"status":"reopened"}
        if args.command == "review-page":
            import_page_review(store, args.document_id, args.page, read_json(args.file), actor=args.actor, reason=args.reason)
            return {"status": "reviewed"}
        if args.command == "review":
            return {"changed": store.review(args.table, args.id, args.state, actor=args.actor, reason=args.reason)}
        if args.command == "export-review":
            target = Path(args.output).resolve()
            # This export contains private evidence; require it to remain inside private runtime.
            if not target.is_relative_to(store.root):
                raise WindError("Review export must stay inside private runtime")
            from .core import atomic_write
            atomic_write(target, canonical({"documents": store.all("documents"), "rows": [dict(r) for r in store.db.execute("SELECT * FROM extracted_rows")],
                "events": store.all("events"), "milestones": store.all("milestones"), "jobs": queue.report(), "gates": publication_gates(store), "review_digests": {d["id"]:review_digest(store,d["id"]) for d in store.all("documents")}}))
            return {"output": str(target)}
        if args.command == "enqueue-projects":
            from .sources import enqueue_project_searches
            return {"jobs": enqueue_project_searches(store, list(read_json(args.sources)), args.end, incremental=args.incremental)}
        if args.command == "collect-notice":
            from .sources import ingest_notice
            return ingest_notice(store, args.url, agency=args.agency, hosts=args.hosts.split(","), title=args.title, quarter=args.quarter)
        if args.command == "enqueue":
            if args.single_window:
                return {"jobs": [queue.enqueue(args.source, args.query, args.start, args.end)]}
            ids = queue.incremental(args.source, args.query, args.end, earliest=args.start) if args.incremental else queue.backfill(args.source, args.query, args.start, args.end)
            return {"jobs": ids}
        if args.command == "refresh-search":
            return {"refreshed": queue.refresh(args.id)}
        if args.command in {"retry", "split"}:
            return {"result": getattr(queue, args.command)(args.id)}
        if args.command == "cycle":
            from .cycle import collection_cycle
            configs = read_json(args.sources)
            if not 1 <= args.max_jobs <= 100 or not 1 <= args.max_pages <= 100:
                raise WindError("Cycle bounds must be 1..100")
            if any(c["kind"] == "open_portal" for c in configs.values()):
                raise WindError("Portal uses collect --portal with its separately initialized browser session")
            return collection_cycle(store, configs, max_jobs=args.max_jobs, max_pages=args.max_pages)
        if args.command == "collect":
            from .sources import run_job, OpenPortalSession
            configs = read_json(args.sources)
            if not 1 <= args.max_pages <= 100:
                raise WindError("max-pages must be 1..100")
            if args.portal:
                if any(c["kind"] != "open_portal" for c in configs.values()):
                    raise WindError("Portal session needs portal-only source config")
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        context = browser.new_context(locale="ko-KR")
                        return run_job(store, configs, max_pages=args.max_pages, fetcher=OpenPortalSession(context).fetch)
                    finally:
                        browser.close()
            return run_job(store, configs, max_pages=args.max_pages)
        if args.command in {"build", "publish"}:
            snap = build_snapshot(store, args.as_of, args.known_at or utc_now(), preview=getattr(args, "preview", False))
            if args.command == "publish":
                from .publication import publish
                return publish(snap, args.clone, expected_remote=args.remote, branch=args.branch)
            result = write_snapshot(snap, args.output, allow_preview=args.preview)
            from .publication import validate_tree
            validate_tree(args.output, allow_preview=args.preview)
            return result
        if args.command == "backup-bundle":
            from .backup import backup_bundle
            return backup_bundle(store, args.output)
        if args.command == "backup":
            return {"backup": store.backup(args.output)}
    raise WindError("Unsupported command")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = execute(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("status") == "blocked" else 0
    except (WindError, KeyError, TypeError, OSError, ValueError, sqlite3.Error) as exc:
        # Never print arbitrary network/SQL payloads or secret environment values.
        print("wind-map: " + (str(exc) if isinstance(exc, WindError) else type(exc).__name__), file=sys.stderr)
        return 2