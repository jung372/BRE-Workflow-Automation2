"""Server-side region totals conserve exact capacity and distinct permit counts."""
from .core import WindError, confirmed_by, watts
from .events import STATUSES, selected_facts


def region_tree(regions):
    result = {r["id"]: dict(r) for r in regions}
    if len(result) != len(regions):
        raise WindError("Duplicate region code")
    result.setdefault("KR", {"id": "KR", "name": "전국", "parent_id": None, "code_year": 2026})
    for key, r in result.items():
        current, seen = key, set()
        while current is not None:
            if current in seen or current not in result:
                raise WindError("Broken or cyclic region hierarchy")
            seen.add(current); current = result[current].get("parent_id")
        if "KR" not in seen:
            raise WindError("Region is disconnected from national root")
    return result


def allocations_for(state, records, regions, as_of, known_at):
    facts = [r for r in selected_facts(records, as_of, known_at) if confirmed_by(r["date"], as_of)]
    if state["capacity_w"] == 0:
        return [{"region_id": "KR", "capacity_w": 0, "unallocated": True, "reason": "zero_capacity_region_unconfirmed"}]
    if not facts:
        return [{"region_id": "KR", "capacity_w": state["capacity_w"], "unallocated": True, "reason": "location_unconfirmed"}]
    facts.sort(key=lambda r: (r["date"]["end"], r["id"]))
    latest = facts[-1]
    same_date = [r for r in facts if r["date"]["end"] == latest["date"]["end"]]
    if len(same_date) > 1:
        raise WindError("Conflicting regional allocations")
    parts = latest["parts"]
    if len({p["region_id"] for p in parts}) != len(parts):
        raise WindError("Duplicate region in allocation")
    if sum(watts(p["capacity_w"]) for p in parts) != state["capacity_w"]:
        raise WindError("Allocation does not equal current permit capacity")
    if latest.get("capacity_event_id") != state["capacity_event_id"]:
        raise WindError("Allocation must reference the current capacity event")
    output = []
    for part in parts:
        if part["region_id"] not in regions:
            raise WindError("Unknown allocation region")
        has_children = any(r.get("parent_id") == part["region_id"] for r in regions.values())
        output.append({"region_id": part["region_id"], "capacity_w": part["capacity_w"], "unallocated": bool(part.get("unallocated") or has_children or part["region_id"] == "KR"), "evidence_id": latest["evidence_id"]})
    return output


def build_totals(states, regions, coverage):
    tree = region_tree(regions)
    coverage_by_region = {r["region_id"]: r for r in coverage}
    scopes = {}
    for scope in (*STATUSES, "all"):
        bins = {key: {"region_id": key, "name": r["name"], "parent_id": r.get("parent_id"), "capacity_w": 0, "unallocated_w": 0, "permit_ids": set(), "unknown_capacity_permits": set(), "coverage": coverage_by_region.get(key, {}).get("status", "not_collected")} for key, r in tree.items()}
        for state in states:
            if scope != "all" and state["status"] != scope:
                continue
            # Zero capacity remains a known permit; no regional inference is made.
            for part in state["allocations"]:
                region = part["region_id"]
                if part["unallocated"]:
                    bins[region]["unallocated_w"] += part["capacity_w"]
                while region:
                    bins[region]["capacity_w"] += part["capacity_w"]
                    bins[region]["permit_ids"].add(state["permit_id"])
                    if state.get("capacity_known") is False:
                        bins[region]["unknown_capacity_permits"].add(state["permit_id"])
                    region = tree[region].get("parent_id")
        for key, item in bins.items():
            children = [r["id"] for r in tree.values() if r.get("parent_id") == key]
            if children or key == "KR":
                if item["capacity_w"] != item["unallocated_w"] + sum(bins[c]["capacity_w"] for c in children):
                    raise WindError("Regional capacity reconciliation failed")
            item["permit_ids"] = sorted(item["permit_ids"])
            item["permit_count"] = len(item["permit_ids"])
            item["unknown_capacity_count"] = len(item.pop("unknown_capacity_permits"))
            item["has_observations"] = bool(item["permit_ids"]) or item["coverage"] == "complete"
            if not item["has_observations"] or (item["unknown_capacity_count"] and item["unknown_capacity_count"] == item["permit_count"]):
                item["display_capacity_w"] = None
            else:
                item["display_capacity_w"] = item["capacity_w"]
        expected = sum(s["capacity_w"] for s in states if scope == "all" or s["status"] == scope)
        if bins["KR"]["capacity_w"] != expected:
            raise WindError("National reconciliation failed")
        scopes[scope] = list(bins.values())
    return scopes