"""Build deterministic, fail-closed public snapshots from one database read view."""
from __future__ import annotations
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from .aggregates import allocations_for, build_totals, region_tree
from .core import WindError, atomic_write, canonical, confirmed_by, day, instant, safe_url, stable_id
from .events import permit_state, selected_facts, validate_lineage
from .geography import coordinate_for_display
from .milestones import validate_milestone, inspection_summary, infer_start_window, annotate_conflicts

PREFIX = "data/wind-map/"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def public_evidence(e, documents):
    doc = documents.get(e["document_id"])
    if not doc or doc["visibility"] != "public" or e.get("publication_approved") is not True:
        raise WindError("Confirmed facts need approved public evidence")
    if not isinstance(e.get("quote"), str) or not e["quote"] or not (e.get("page") or e.get("document_number") or e.get("section")):
        raise WindError("Evidence needs a quote and a document locator")
    if e.get("page") is not None and (type(e["page"]) is not int or e["page"] < 1 or (doc.get("page_count") and e["page"] > doc["page_count"])):
        raise WindError("Invalid evidence page")
    if any(e.get(k) is not None and not isinstance(e[k], str) for k in ("document_number", "section")):
        raise WindError("Invalid evidence locator")
    return {"id": e["id"], "agency": doc["agency"], "title": doc["title"], "url": safe_url(doc["url"]), "document_number": e.get("document_number"), "page": e.get("page"), "section": e.get("section"), "quote": e["quote"], "checked_at": doc["checked_at"], "source_sha256": doc["sha256"]}


def review_digest(store, document_id):
    pages = [dict(r) for r in store.db.execute("SELECT * FROM pages WHERE document_id=? ORDER BY page", (document_id,))]
    rows = [dict(r) for r in store.db.execute("SELECT * FROM extracted_rows WHERE document_id=? ORDER BY page,position", (document_id,))]
    return hashlib.sha256(canonical({"document": store.document(document_id), "pages": pages, "rows": rows})).hexdigest()


def publication_gates(store):
    errors = []
    documents = store.all("documents")
    registers = [d for d in documents if d.get("purpose") == "permit_register"]
    if not registers:
        errors.append("No complete permit-register source")
    validations = store.all("validations")
    permits = {p["id"] for p in store.all("permits")}
    evidence = {e["id"]: e for e in store.all("evidence")}
    for doc in registers:
        expected = doc.get("page_count")
        pages = list(store.db.execute("SELECT page,status,reviewed FROM pages WHERE document_id=? ORDER BY page", (doc["id"],)))
        if type(expected) is not int or expected < 1 or [r["page"] for r in pages] != list(range(1, (expected or 0) + 1)):
            errors.append("Incomplete pages: " + doc["id"])
        if any(r["status"] not in ("extracted", "blank") or not r["reviewed"] for r in pages):
            errors.append("Unreviewed pages: " + doc["id"])
        rows = list(store.db.execute("SELECT id,classification FROM extracted_rows WHERE document_id=?", (doc["id"],)))
        if any(r["classification"] == "review" for r in rows):
            errors.append("Unclassified register rows: " + doc["id"])
        candidates = {r["id"] for r in rows if r["classification"] == "candidate"}
        digest = review_digest(store, doc["id"])
        signed = [v for v in validations if v.get("document_id") == doc["id"] and v.get("approved") is True
                  and v.get("source_sha256") == doc["sha256"] and v.get("review_digest") == digest and v.get("actor") and v.get("checked_at")]
        approved = set()
        for v in signed:
            try:
                instant(v["checked_at"])
                if v["kind"] == "row_reconciliation":
                    links = v.get("row_links", [])
                    if {x["row_id"] for x in links} != candidates or len(links) != len(candidates):
                        raise WindError("Candidate rows are not fully reconciled")
                    for x in links:
                        e = evidence.get(x["evidence_id"])
                        if x["permit_id"] not in permits or not e or e["document_id"] != doc["id"] or e.get("publication_approved") is not True:
                            raise WindError("Row-to-permit evidence is missing")
                elif v["kind"] == "golden_samples":
                    checks = v.get("checks", [])
                    if not checks:
                        raise WindError("No golden assertions")
                    for check in checks:
                        if check["table"] not in {"events", "permits", "allocation_sets", "milestones"}:
                            raise WindError("Unsupported golden assertion")
                        row = next((r for r in store.all(check["table"]) if r["id"] == check["record_id"]), None)
                        if row is None:
                            raise WindError("Missing golden record")
                        value = row
                        for field in check["field"].split("."):
                            value = value[field]
                        if value != check["expected"]:
                            raise WindError("Golden assertion mismatch")
                else:
                    continue
                approved.add(v["kind"])
            except (WindError, KeyError, TypeError, StopIteration):
                continue
        if not {"golden_samples", "row_reconciliation"} <= approved:
            errors.append("Missing or stale signed-off golden samples/row reconciliation: " + doc["id"])
    if not store.all("coverage"):
        errors.append("Collection coverage is missing")
    return errors


def _build(store, as_of, known_at, *, preview=False):
    day(as_of); instant(known_at)
    gates = publication_gates(store)
    if gates and not preview:
        raise WindError("Publication blocked: " + "; ".join(gates))
    docs = {d["id"]: d for d in store.all("documents")}
    evidence = {e["id"]: e for e in store.all("evidence")}
    projects = {p["id"]: p for p in store.all("projects")}
    regions = region_tree([{k: row.get(k) for k in ("id", "name", "parent_id", "code_year")} for row in store.all("regions")])
    for region in regions.values():
        if not ID_RE.fullmatch(region["id"]) or not isinstance(region["name"], str) or type(region["code_year"]) is not int:
            raise WindError("Invalid public region fields")
    permits = store.all("permits")
    events = store.all("events")
    allocation_sets = store.all("allocation_sets")
    milestones = store.all("milestones")
    coordinates = store.all("coordinates")
    for ident in [*projects, *evidence, *[p["id"] for p in permits]]:
        if not ID_RE.fullmatch(ident):
            raise WindError("Unsafe public ID")
    events_by = defaultdict(list); allocations_by = defaultdict(list)
    for event in events:
        events_by[event["permit_id"]].append(event)
    for allocation in allocation_sets:
        allocations_by[allocation["permit_id"]].append(allocation)
    states = []
    public_sources = {}
    def use_evidence(ident):
        if ident not in evidence:
            raise WindError("Missing evidence")
        public_sources[ident] = public_evidence(evidence[ident], docs)
        if instant(public_sources[ident]["checked_at"]) > instant(known_at):
            raise WindError("Evidence was unavailable at the requested knowledge cutoff")
    for permit in permits:
        if projects[permit["project_id"]].get("wind_type") != "onshore":
            continue
        state = permit_state(permit, events_by[permit["id"]], as_of, known_at)
        if not state["history"] and not state["conflicts"]:
            continue
        for ident in state["evidence_ids"]:
            use_evidence(ident)
        state["allocations"] = allocations_for(state, allocations_by[permit["id"]], regions, as_of, known_at)
        for part in state["allocations"]:
            if part.get("evidence_id"):
                use_evidence(part["evidence_id"])
        states.append(state)
    validate_lineage(states, events, as_of, known_at)
    if not states:
        raise WindError("No supported permit facts; do not publish an invented zero")
    coverage = [{k: row.get(k) for k in ("id", "region_id", "status", "as_of", "scope", "quarter")} for row in store.all("coverage")]
    for c in coverage:
        if c["region_id"] not in regions or c["status"] not in {"complete", "central_only", "partial", "not_collected"}:
            raise WindError("Invalid collection coverage")
        day(c["as_of"])
    totals = build_totals(states, list(regions.values()), coverage)
    details, index, inspected_states = {}, [], []
    for project_id in sorted({s["project_id"] for s in states}):
        project = projects[project_id]
        if not isinstance(project.get("name"), str) or not project["name"] or not project.get("name_evidence_id"):
            raise WindError("Project display name needs evidence")
        use_evidence(project["name_evidence_id"])
        selected = [s for s in states if s["project_id"] == project_id]
        applicable = {s["permit_id"] for s in selected}
        milestone_rows = []
        for m in selected_facts([m for m in milestones if m["project_id"] == project_id], as_of, known_at):
            if m["date"]["start"] and m["date"]["start"] > as_of:
                continue
            validate_milestone(m, applicable); use_evidence(m["evidence_id"])
            milestone_rows.append({k: m[k] for k in ("id", "type", "status", "date", "permit_ids", "evidence_id", "assessment_type", "stage", "round", "equipment", "disposition_kind") if k in m})
        annotate_conflicts(milestone_rows)
        point, coordinate_warning = None, None
        coords = [c for c in selected_facts([c for c in coordinates if c["project_id"] == project_id], as_of, known_at) if confirmed_by(c["date"], as_of)]
        if coords:
            coords.sort(key=lambda c: (c["date"]["end"], c["id"]))
            try:
                if len([c for c in coords if c["date"]["end"] == coords[-1]["date"]["end"]]) > 1:
                    raise WindError("Conflicting coordinates")
                point = coordinate_for_display(coords[-1]); use_evidence(coords[-1]["evidence_id"])
            except WindError as exc:
                coordinate_warning = str(exc)
        source_ids = {project["name_evidence_id"]}
        for s in selected:
            source_ids.update(s["evidence_ids"])
            source_ids.update(a["evidence_id"] for a in s["allocations"] if a.get("evidence_id"))
        source_ids.update(m["evidence_id"] for m in milestone_rows)
        if point:
            source_ids.add(point["evidence_id"])
        aliases = []
        for alias in project.get("aliases", []):
            if not isinstance(alias, dict) or not alias.get("evidence_id"):
                raise WindError("Aliases require scoped evidence")
            if not isinstance(alias.get("name"), str):
                raise WindError("Invalid project alias")
            for boundary in ("from", "until"):
                if alias.get(boundary):
                    day(alias[boundary])
            use_evidence(alias["evidence_id"]); source_ids.add(alias["evidence_id"])
            aliases.append({k: alias.get(k) for k in ("name", "from", "until", "evidence_id")})
        equipment = project.get("equipment", [])
        for unit in equipment:
            use_evidence(unit["evidence_id"]); source_ids.add(unit["evidence_id"])
        inspected = inspection_summary(milestone_rows, equipment, as_of)
        for unit in equipment:
            if unit["id"] in inspected["passed_equipment"]:
                region_id = unit.get("region_id", "KR")
                if region_id not in regions:
                    raise WindError("Unknown inspected equipment region")
                has_children = any(r.get("parent_id") == region_id for r in regions.values())
                inspected_states.append({"permit_id": project_id + ":" + unit["id"], "status": "valid",
                    "capacity_w": unit["capacity_w"], "allocations": [{"region_id": region_id,
                    "capacity_w": unit["capacity_w"], "unallocated": region_id == "KR" or has_children}]})
        start_window = None
        negatives = [m for m in milestone_rows if m["type"] == "construction" and m["status"] == "not_started" and confirmed_by(m["date"], as_of)]
        positives = [m for m in milestone_rows if m["type"] == "construction" and m["status"] == "in_progress" and confirmed_by(m["date"], as_of)]
        for positive in sorted(positives, key=lambda m: m["date"]["start"]):
            before = [m for m in negatives if set(m["permit_ids"]) == set(positive["permit_ids"]) and m["date"]["end"] < positive["date"]["start"]]
            if before:
                start_window = infer_start_window(max(before, key=lambda m: m["date"]["end"]), positive)
                break
        detail = {"project_id": project_id, "inspection": inspected, "construction_start_window": start_window, "name": project["name"], "name_kind": project.get("name_kind", "permitted_project"), "aliases": aliases, "permits": selected, "milestones": milestone_rows, "coordinate": point, "coordinate_warning": coordinate_warning, "sources": [public_sources[e] for e in sorted(source_ids)]}
        details[project_id] = detail
        index.append({"project_id": project_id, "name": project["name"], "aliases": [a["name"] for a in aliases], "coordinate": point, "inspection": inspected, "permits": [{k: s[k] for k in ("permit_id", "status", "capacity_w", "allocations")} for s in selected]})
    pending = sum(r.get("review_state") == "pending" for r in events + milestones + coordinates + allocation_sets)
    inspection_totals = build_totals(inspected_states, list(regions.values()), [])["valid"]
    quarters = {d["quarter"] for d in docs.values() if d.get("quarter")}
    if any(not isinstance(q, str) or not re.fullmatch(r"20\d{2}-Q[1-4]", q) for q in quarters):
        raise WindError("Invalid source quarter")
    meta = {"inspection_totals": inspection_totals, "schema_version": 1, "as_of": as_of, "known_at": known_at, "preview": preview, "coverage": coverage, "pending_count": pending, "gate_errors": gates if preview else [], "quarters": sorted(quarters), "regions": list(regions.values())}
    # Snapshot identity includes source details and all outputs, not wall-clock build time.
    snapshot_id = hashlib.sha256(canonical({"meta": meta, "totals": totals, "index": index, "details": details})).hexdigest()[:24]
    envelope = {"schema_version": 1, "snapshot_id": snapshot_id}
    files = {"regions.json": {**envelope, **meta, "totals": totals}, "index.json": {**envelope, "projects": index}}
    for key, value in details.items():
        files[f"projects/{key}.json"] = {**envelope, **value}
    manifest = {**envelope, "as_of": as_of, "known_at": known_at, "preview": preview, "files": {path: hashlib.sha256(canonical(value)).hexdigest() for path, value in files.items()}}
    files["manifest.json"] = manifest
    return {"snapshot_id": snapshot_id, "manifest": manifest, "files": {path: canonical(value) for path, value in files.items()}}


def build_snapshot(store, as_of, known_at, *, preview=False):
    # One consistent database view, even while another process queues source jobs.
    with store.transaction():
        return _build(store, as_of, known_at, preview=preview)


def write_snapshot(snapshot, output, *, allow_preview=False):
    if not re.fullmatch(r"[a-f0-9]{24}", snapshot.get("snapshot_id", "")):
        raise WindError("Invalid snapshot ID")
    if snapshot["manifest"]["preview"] and not allow_preview:
        raise WindError("Preview cannot be published")
    supplied_root = Path(output).absolute()
    if any(p.is_symlink() for p in [supplied_root, *supplied_root.parents]):
        raise WindError("Symlink publication root")
    root = supplied_root.resolve()
    version = root / PREFIX / "versions" / snapshot["snapshot_id"]
    current_path = root / PREFIX / "current.json"
    previous = None
    if any(p.is_symlink() for p in [current_path, *current_path.parents]):
        raise WindError("Unsafe current pointer path")
    if current_path.exists():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        previous = current.get("current")
        if previous and previous["snapshot_id"] == snapshot["snapshot_id"]:
            previous = current.get("previous")
    for relative, content in snapshot["files"].items():
        target = version / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root) or any(p.is_symlink() for p in target.parents if p.is_relative_to(root)):
            raise WindError("Unsafe snapshot path")
        if target.exists() and target.read_bytes() != content:
            raise WindError("Immutable snapshot conflict")
    for relative, content in snapshot["files"].items():
        target = version / relative
        if not target.exists():
            atomic_write(target, content)
    descriptor = {"snapshot_id": snapshot["snapshot_id"], "manifest": PREFIX + "versions/" + snapshot["snapshot_id"] + "/manifest.json", "sha256": hashlib.sha256(snapshot["files"]["manifest.json"]).hexdigest()}
    from .publication import validate_descriptor
    validate_descriptor(root, descriptor, allow_preview=allow_preview)
    pointer = {"schema_version": 1, "current": descriptor, "previous": previous}
    atomic_write(current_path, canonical(pointer))
    return pointer