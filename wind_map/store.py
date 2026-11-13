"""Private SQLite store, immutable originals, audited review and consistent backup."""
from __future__ import annotations
from contextlib import contextmanager, closing
import hashlib
import json
from pathlib import Path
import sqlite3
from .core import WindError, atomic_write, canonical, instant, safe_url, stable_id, utc_now

TABLES = {"projects", "evidence", "permits", "events", "allocation_sets", "milestones", "coordinates", "regions", "coverage", "validations"}
COLUMNS = {"evidence": ("document_id",), "permits": ("project_id", "authority", "number"), "events": ("permit_id", "evidence_id"), "allocation_sets": ("permit_id", "evidence_id"), "milestones": ("project_id", "evidence_id"), "coordinates": ("project_id", "evidence_id"), "validations": ("document_id", "kind", "approved")}

class Store:
    def __init__(self, runtime):
        self.root = Path(runtime).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "wind-map.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        if self.db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
            self.close(); raise WindError("Unsupported database schema")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback(); raise
        else:
            self.db.commit()

    def register_document(self, content, metadata):
        meta = dict(metadata)
        for key in ("source_key", "agency", "title", "url", "checked_at", "visibility", "format"):
            if not isinstance(meta.get(key), str) or not meta[key]:
                raise WindError("Missing document metadata: " + key)
        safe_url(meta["url"]); instant(meta["checked_at"])
        if meta["visibility"] not in ("public", "restricted"):
            raise WindError("Unknown visibility")
        digest = hashlib.sha256(content).hexdigest()
        ident = stable_id("doc", [meta["source_key"], digest])
        asset = self.root / "originals" / digest[:2] / digest
        if asset.exists() and hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
            raise WindError("Original asset hash mismatch")
        if not asset.exists():
            atomic_write(asset, content)
        meta.update(id=ident, sha256=digest, asset=str(asset.relative_to(self.root)))
        with self.transaction():
            old = self.db.execute("SELECT data FROM documents WHERE id=?", (ident,)).fetchone()
            if not old:
                self.db.execute("INSERT INTO documents VALUES (?,?,?,?)", (ident, meta["source_key"], digest, canonical(meta).decode()))
        return ident

    def document(self, ident):
        row = self.db.execute("SELECT data FROM documents WHERE id=?", (ident,)).fetchone()
        if not row:
            raise WindError("Unknown document")
        return json.loads(row[0])

    def amend_document_metadata(self, ident, changes, *, actor, reason):
        allowed = {"agency", "title", "checked_at", "visibility", "purpose", "quarter", "page_count", "previous_version"}
        if not changes or set(changes) - allowed or not actor.strip() or not reason.strip():
            raise WindError("Document metadata corrections require explicit fields, actor and reason")
        old = self.document(ident)
        value = {**old, **changes}
        instant(value["checked_at"])
        if value["visibility"] not in {"public", "restricted"} or any(not isinstance(value[k], str) or not value[k] for k in ("agency", "title")):
            raise WindError("Invalid corrected document metadata")
        if "page_count" in value and (type(value["page_count"]) is not int or value["page_count"] < 1):
            raise WindError("Invalid corrected page count")
        with self.transaction():
            self.db.execute("UPDATE documents SET data=? WHERE id=?", (canonical(value).decode(), ident))
            self.db.execute("INSERT INTO review_log(entity_type,entity_id,actor,action,reason,at,previous,current) VALUES (?,?,?,?,?,?,?,?)",
                ("documents", ident, actor, "metadata_correction", reason, utc_now(), canonical(old).decode(), canonical(value).decode()))
        return value

    def all(self, table):
        if table not in TABLES | {"documents"}:
            raise WindError("Unknown table")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT data FROM {table} ORDER BY id")]

    def put(self, table, record, *, actor, reason):
        if table not in TABLES or not actor.strip() or not reason.strip():
            raise WindError("Reviewed imports require actor and reason")
        value = dict(record)
        if not isinstance(value.get("id"), str) or not value["id"]:
            raise WindError("Record ID required")
        if table in {"events", "allocation_sets", "milestones", "coordinates"}:
            if value.get("review_state") not in {"pending", "approved", "rejected"}:
                raise WindError("Explicit review_state required")
            instant(value["known_at"])
        columns = COLUMNS.get(table, ())
        values = [value["id"], *[value.get(c) for c in columns], canonical(value).decode()]
        old = self.db.execute(f"SELECT data FROM {table} WHERE id=?", (value["id"],)).fetchone()
        if old and json.loads(old[0]) == value:
            return False
        # Source assertions/events are append-only; corrections reference the old ID.
        if old and table in {"events", "evidence", "milestones", "allocation_sets", "coordinates"}:
            previous = json.loads(old[0])
            if {k: v for k, v in previous.items() if k != "review_state"} != {k: v for k, v in value.items() if k != "review_state"}:
                raise WindError("Evidence and facts are immutable; create a correction with supersedes")
        names = ["id", *columns, "data"]
        self.db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)}) ON CONFLICT(id) DO UPDATE SET " + ",".join(f"{n}=excluded.{n}" for n in names[1:]), values)
        self.db.execute("INSERT INTO review_log(entity_type,entity_id,actor,action,reason,at,previous,current) VALUES (?,?,?,?,?,?,?,?)", (table, value["id"], actor, "update" if old else "insert", reason, utc_now(), old[0] if old else None, canonical(value).decode()))
        return True

    def import_bundle(self, bundle, *, actor, reason):
        if set(bundle) - TABLES - {"schema_version"} or bundle.get("schema_version") != 1:
            raise WindError("Unknown bundle schema or section")
        order = ("projects", "regions", "evidence", "permits", "events", "allocation_sets", "milestones", "coordinates", "coverage", "validations")
        count = 0
        with self.transaction():
            for table in order:
                for record in bundle.get(table, []):
                    count += self.put(table, record, actor=actor, reason=reason)
        return count

    def review(self, table, ident, state, *, actor, reason):
        if table not in {"events", "allocation_sets", "milestones", "coordinates"} or state not in {"approved", "rejected", "pending"}:
            raise WindError("Invalid review target")
        row = next((v for v in self.all(table) if v["id"] == ident), None)
        if row is None:
            raise WindError("Unknown review record")
        row["review_state"] = state
        with self.transaction():
            return self.put(table, row, actor=actor, reason=reason)

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination.exists():
            raise WindError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            self.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WindError("Backup integrity check failed")
        return str(destination)

    def record_page(self, document_id, number, rows, *, status, reviewed=False, metadata=None):
        if status not in {"extracted", "needs_ocr", "needs_review", "blank", "failed"}:
            raise WindError("Unknown page status")
        with self.transaction():
            prior = self.db.execute("SELECT reviewed FROM pages WHERE document_id=? AND page=?", (document_id, number)).fetchone()
            if prior and prior[0]:
                return  # Re-extraction never overwrites an operator's reviewed page.
            self.db.execute("INSERT INTO pages VALUES (?,?,?,?,?) ON CONFLICT(document_id,page) DO UPDATE SET status=excluded.status,reviewed=excluded.reviewed,data=excluded.data", (document_id, number, status, int(reviewed), canonical(metadata or {}).decode()))
            self.db.execute("DELETE FROM extracted_rows WHERE document_id=? AND page=?", (document_id, number))
            for index, row in enumerate(rows):
                classification = row.get("classification", "review")
                if classification not in {"candidate", "out_of_scope", "header", "blank", "review"}:
                    raise WindError("Unknown row classification")
                ident = stable_id("row", [document_id, number, index])
                self.db.execute("INSERT INTO extracted_rows VALUES (?,?,?,?,?,?)", (ident, document_id, number, index, classification, canonical(dict(row, id=ident)).decode()))

    def approve_page(self, document_id, number, *, actor, reason):
        if not actor.strip() or not reason.strip():
            raise WindError("Page review needs actor and reason")
        with self.transaction():
            row = self.db.execute("SELECT * FROM pages WHERE document_id=? AND page=?", (document_id, number)).fetchone()
            if not row or row["status"] not in {"extracted", "blank"}:
                raise WindError("Unresolved or unprocessed page")
            if self.db.execute("SELECT 1 FROM extracted_rows WHERE document_id=? AND page=? AND classification='review'", (document_id, number)).fetchone():
                raise WindError("Unclassified rows remain")
            self.db.execute("UPDATE pages SET reviewed=1 WHERE document_id=? AND page=?", (document_id, number))
            self.db.execute("INSERT INTO review_log(entity_type,entity_id,actor,action,reason,at,current) VALUES (?,?,?,?,?,?,?)", ("pages", f"{document_id}:{number}", actor, "approve", reason, utc_now(), "{}"))

    def reopen_page(self, document_id, number, *, actor, reason):
        if not actor.strip() or not reason.strip():
            raise WindError("Reopening a reviewed page requires actor and reason")
        with self.transaction():
            page = self.db.execute("SELECT * FROM pages WHERE document_id=? AND page=?", (document_id,number)).fetchone()
            if not page or not page["reviewed"]:
                raise WindError("Page is not reviewed")
            rows = [dict(r) for r in self.db.execute("SELECT * FROM extracted_rows WHERE document_id=? AND page=? ORDER BY position",(document_id,number))]
            previous = {"page":dict(page),"rows":rows}
            self.db.execute("UPDATE pages SET reviewed=0 WHERE document_id=? AND page=?",(document_id,number))
            self.db.execute("INSERT INTO review_log(entity_type,entity_id,actor,action,reason,at,previous,current) VALUES (?,?,?,?,?,?,?,?)",
                ("pages",f"{document_id}:{number}",actor,"reopen",reason,utc_now(),canonical(previous).decode(),canonical({"reviewed":False}).decode()))
