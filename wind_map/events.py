"""Replay reviewed permit facts; collection order never defines legal chronology."""
from collections import defaultdict
from .core import WindError, confirmed_by, day, instant, validate_date, watts

CAPACITY_TYPES = {"grant", "capacity_change", "registry_observation"}
EVENT_TYPES = CAPACITY_TYPES | {"extend", "rename", "transfer", "cancel", "split_close", "merge_close", "approved_pending", "business_commenced", "validity_confirmed"}
STATUSES = ("valid", "review", "approved_pending", "cancelled")


def selected_facts(records, as_of, known_at):
    day(as_of); cutoff = instant(known_at)
    reviewed = []
    by_id = {r["id"]: r for r in records}
    if len(by_id) != len(records):
        raise WindError("Duplicate fact ID")
    for r in records:
        if r.get("review_state") != "approved" or instant(r["known_at"]) > cutoff:
            continue
        validate_date(r["date"])
        reviewed.append(r)
    replaced = set()
    for r in reviewed:
        prior = r.get("supersedes")
        if prior:
            if prior not in by_id or prior == r["id"]:
                raise WindError("Unknown correction target")
            old = by_id[prior]
            if old.get("permit_id") != r.get("permit_id") or old.get("project_id") != r.get("project_id") or old.get("type") != r.get("type"):
                raise WindError("Correction changed fact identity")
            seen = {r["id"]}
            while prior:
                if prior in seen or prior not in by_id:
                    raise WindError("Cyclic or broken correction chain")
                seen.add(prior); prior = by_id[prior].get("supersedes")
            replaced.update(seen - {r["id"]})
    return [r for r in reviewed if r["id"] not in replaced]


def permit_state(permit, records, as_of, known_at):
    if any(not isinstance(permit.get(k, ""), str) for k in ("id", "project_id", "authority", "number", "name")):
        raise WindError("Permit identifiers and names must be text")
    facts = selected_facts(records, as_of, known_at)
    active, uncertain = [], []
    for r in facts:
        if any(k in r and not isinstance(r[k], str) for k in ("name", "operator")):
            raise WindError("Event names must be text")
        if "successors" in r and (not isinstance(r["successors"], list) or any(not isinstance(x, str) for x in r["successors"])):
            raise WindError("Invalid successor identifiers")
        if r["type"] not in EVENT_TYPES:
            raise WindError("Unsupported permit event")
        if r["type"] in CAPACITY_TYPES | {"approved_pending"}:
            watts(r["capacity_w"])
        if confirmed_by(r["date"], as_of):
            active.append(r)
        elif r["date"]["start"] is None or r["date"]["start"] <= as_of:
            uncertain.append(r)
    active.sort(key=lambda r: (r["date"]["end"], r["id"]))
    result = {"permit_id": permit["id"], "project_id": permit["project_id"], "authority": permit["authority"], "number": permit["number"], "status": "review", "capacity_w": 0, "capacity_known": False, "first_ebl_date": None, "capacity_ebl_date": None, "preparation_deadline": None, "construction_plan_deadline": None, "operator": None, "capacity_event_id": None, "evidence_ids": [], "conflicts": [], "name": permit.get("name", ""), "history": []}
    begun = False
    cancelled = False
    # Different capacity/disposition facts on one effective day cannot be ordered by ID.
    same_day = defaultdict(list)
    for r in active:
        if r["type"] in CAPACITY_TYPES | {"cancel", "split_close", "merge_close"}:
            same_day[r["date"]["end"]].append(r)
    conflicts = {k for k, group in same_day.items() if len({(r["type"], r.get("capacity_w")) for r in group}) > 1}
    for r in active:
        result["history"].append({k: r[k] for k in ("id", "type", "date", "evidence_id", "capacity_w", "previous_capacity_w", "preparation_deadline", "construction_plan_deadline", "name", "operator", "successors") if k in r})
        result["evidence_ids"].append(r["evidence_id"])
        if r["date"]["end"] in conflicts:
            result["conflicts"].append("same_effective_date:" + r["date"]["end"])
            continue
        t = r["type"]
        if t in CAPACITY_TYPES:
            if cancelled:
                result["conflicts"].append("capacity_after_cancellation:" + r["id"])
                continue
            old_capacity = result["capacity_w"]
            capacity_event = result["capacity_event_id"] if t == "registry_observation" and result["capacity_known"] and old_capacity == r["capacity_w"] else r["id"]
            if t == "registry_observation" and result["capacity_known"] and old_capacity != r["capacity_w"]:
                result["capacity_ebl_date"] = None
            result.update(capacity_w=r["capacity_w"], capacity_known=True, status="valid", capacity_event_id=capacity_event)
            if r.get("operator"):
                result["operator"] = r["operator"]
            if t == "grant" and r.get("is_initial") is True:
                if result["first_ebl_date"] and result["first_ebl_date"] != r["date"]:
                    result["conflicts"].append("multiple_initial_grants")
                else:
                    result["first_ebl_date"] = r["date"]
                    result["capacity_ebl_date"] = r["date"]
            elif t == "capacity_change":
                if not r.get("previous_capacity_w") == old_capacity:
                    result["conflicts"].append("capacity_change_baseline_mismatch")
                if old_capacity != r["capacity_w"]:
                    result["capacity_ebl_date"] = r["date"]
            elif t == "registry_observation":
                # A register's reporting date is never a grant/change date.
                if r.get("first_ebl_date"):
                    validate_date(r["first_ebl_date"])
                    result["first_ebl_date"] = r["first_ebl_date"]
                if r.get("capacity_ebl_date"):
                    validate_date(r["capacity_ebl_date"])
                    result["capacity_ebl_date"] = r["capacity_ebl_date"]
            if "construction_plan_deadline" in r:
                result["construction_plan_deadline"] = validate_date(r["construction_plan_deadline"])
            if "preparation_deadline" in r:
                result["preparation_deadline"] = validate_date(r["preparation_deadline"])
        elif t == "approved_pending" and not result["capacity_known"]:
            result.update(status="approved_pending", capacity_w=r["capacity_w"], capacity_known=True, capacity_event_id=r["id"])
        elif t in {"cancel", "split_close", "merge_close"}:
            result.update(status="cancelled", capacity_w=0, capacity_known=True, capacity_event_id=r["id"])
            cancelled = True
        elif t == "extend":
            result["preparation_deadline"] = validate_date(r["preparation_deadline"])
        elif t == "rename":
            result["name"] = r["name"]
        elif t == "transfer":
            if not r.get("operator"):
                raise WindError("Transfer requires successor operator")
            result["operator"] = r["operator"]
        elif t == "business_commenced":
            begun = True
        elif t == "validity_confirmed" and not cancelled:
            day(r["valid_through"])
            if r["valid_through"] >= as_of:
                begun = True
    deadline = result["preparation_deadline"]
    if deadline and deadline["end"] and deadline["end"] < as_of and not begun and not cancelled:
        result["conflicts"].append("preparation_period_expired_requires_followup")
    if uncertain:
        result["conflicts"].append("effective_date_unresolved")
        result["evidence_ids"].extend(r["evidence_id"] for r in uncertain)
        unresolved_capacity = [r for r in uncertain if r["type"] in CAPACITY_TYPES | {"approved_pending"}]
        if not cancelled and not result["capacity_known"] and len({r["capacity_w"] for r in unresolved_capacity}) == 1:
            r = unresolved_capacity[-1]
            result.update(capacity_w=r["capacity_w"], capacity_known=True, capacity_event_id=r["id"])
    if result["conflicts"] and not cancelled:
        result["status"] = "review"
    result["conflicts"] = sorted(set(result["conflicts"]))
    result["evidence_ids"] = sorted(set(result["evidence_ids"]))
    return result


def validate_lineage(states, events, as_of, known_at):
    by_id = {s["permit_id"]: s for s in states}
    groups = defaultdict(list)
    lineage_facts = selected_facts(events, as_of, known_at)
    for event in lineage_facts:
        if event["type"] in {"split_close", "merge_close"} and confirmed_by(event["date"], as_of):
            if not event.get("lineage_id") or not event.get("successors"):
                raise WindError("Split/merge requires an explicit lineage")
            groups[event["lineage_id"]].append(event)
    for group in groups.values():
        successors = set(group[0]["successors"])
        if any(set(e["successors"]) != successors for e in group) or successors & {e["permit_id"] for e in group}:
            raise WindError("Inconsistent permit lineage")
        if not successors <= by_id.keys():
            raise WindError("Unresolved successor permit")
        # Capacity at the lineage event, not today's later amended capacity.
        end = group[0]["date"]["end"]
        if any(e["date"]["end"] != end for e in group):
            raise WindError("Lineage dates disagree")
        for e in group:
            prior = [r for r in lineage_facts if r["permit_id"] == e["permit_id"] and r["date"]["end"] and r["date"]["end"] < e["date"]["start"]]
            identity = {"id":e["permit_id"],"project_id":"","authority":"","number":""}
            before = permit_state(identity,prior,end,known_at)
            if not before["capacity_known"] or before["capacity_w"] != watts(e["previous_capacity_w"]):
                raise WindError("Lineage baseline does not match predecessor capacity")
        expected = sum(watts(e["previous_capacity_w"]) for e in group)
        actual = 0
        for successor in successors:
            grants = [e for e in lineage_facts if e["permit_id"] == successor and e.get("lineage_id") == group[0]["lineage_id"] and e["type"] == "grant" and e.get("review_state") == "approved" and e["date"]["end"] == end and instant(e["known_at"]) <= instant(known_at)]
            if len(grants) != 1:
                raise WindError("Successor grant is missing or ambiguous")
            actual += watts(grants[0]["capacity_w"])
        if actual != expected:
            raise WindError("Split/merge capacity is not conserved")