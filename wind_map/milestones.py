"""Milestone wording derives only from scoped, reviewed evidence."""
from .core import WindError, date_value, day, validate_date

KINDS = {"construction_plan", "environment", "development", "construction", "inspection", "completion"}
STATES = {"unknown", "not_applied", "applied", "reviewing", "conditional", "approved", "planned", "notice_received", "not_started", "in_progress", "completed", "passed", "failed", "conflict"}


def validate_milestone(row, permit_ids):
    if row["type"] not in KINDS or row["status"] not in STATES:
        raise WindError("Unknown milestone type/status")
    validate_date(row["date"])
    if not row.get("permit_ids") or not set(row["permit_ids"]) <= permit_ids:
        raise WindError("Milestone must name its applicable permits")
    if row["type"] == "environment":
        if row.get("assessment_type") not in {"main", "small", "strategic"} or row.get("stage") not in {"draft", "main", "reconsultation", "change"}:
            raise WindError("Environmental assessment scope is required")
    if row["type"] == "inspection" and (type(row.get("round")) is not int or row["round"] < 1 or not isinstance(row.get("equipment"), list) or not row["equipment"]):
        raise WindError("Inspection round and equipment scope required")
    if row["type"] == "inspection" and (any(not isinstance(x, str) or not x for x in row["equipment"]) or len(set(row["equipment"])) != len(row["equipment"])):
        raise WindError("Inspection equipment IDs must be distinct strings")
    if row["status"] == "not_applied" and not row.get("explicit_negative"):
        raise WindError("No search result is not evidence of non-application")
    return row


def infer_start_window(not_started, in_progress):
    """Return (lower, upper] evidence bounds, never an invented day."""
    if not_started["status"] != "not_started" or in_progress["status"] != "in_progress":
        raise WindError("Wrong construction evidence states")
    lower, upper = not_started["date"]["start"], in_progress["date"]["end"]
    if not lower or not upper or day(lower) >= day(upper):
        raise WindError("Construction evidence does not bound a valid interval")
    value = date_value(lower, end=upper, precision="range", estimated=True)
    return {"date": value, "lower_exclusive": True, "inferred_month": lower[:7] if lower[:7] == upper[:7] else None, "evidence_ids": [not_started["evidence_id"], in_progress["evidence_id"]]}


def inspection_status(milestones, expected_equipment):
    passed = set()
    for m in milestones:
        if m["type"] == "inspection" and m["status"] == "passed":
            passed.update(m["equipment"])
    return {"passed_equipment": sorted(passed), "all_passed": bool(expected_equipment) and set(expected_equipment) <= passed}

def inspection_summary(milestones, equipment, as_of):
    """Count identified equipment once; a later failed test invalidates its passed state."""
    from .core import confirmed_by, watts
    units = {u["id"]: u for u in equipment}
    if len(units) != len(equipment):
        raise WindError("Duplicate equipment ID")
    for unit in units.values():
        watts(unit["capacity_w"])
        if not unit.get("evidence_id"):
            raise WindError("Equipment capacity requires evidence")
    latest = {}
    for m in sorted(milestones, key=lambda r: (r["date"]["end"] or "", r["id"])):
        if m["type"] != "inspection" or m["status"] not in {"passed", "failed"} or not confirmed_by(m["date"], as_of):
            continue
        for ident in m["equipment"]:
            prior = latest.get(ident)
            if prior and prior["date"]["end"] == m["date"]["end"] and prior["status"] != m["status"]:
                raise WindError("Conflicting inspection verdicts")
            latest[ident] = m
    passed = sorted(k for k, m in latest.items() if m["status"] == "passed")
    supported = [units[k] for k in passed if k in units]
    return {"passed_equipment": passed,
            "all_passed": bool(units) and set(units) <= set(passed),
            "capacity_w": sum(u["capacity_w"] for u in supported) if passed and len(supported) == len(passed) else None,
            "evidence_ids": sorted({latest[k]["evidence_id"] for k in passed})}


def annotate_conflicts(rows):
    """Keep both sources when the same scoped occurrence gives incompatible dates."""
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        key = (row["type"], row["status"], tuple(sorted(row["permit_ids"])),
               row.get("assessment_type"), row.get("stage"), row.get("round"),
               tuple(sorted(row.get("equipment", []))), row.get("disposition_kind"))
        groups[key].append(row)
    for group in groups.values():
        dates = {(r["date"]["start"], r["date"]["end"], r["date"]["precision"]) for r in group if r["date"]["precision"] != "unknown"}
        if len(dates) > 1:
            for row in group:
                row["conflict"] = True
    return rows
