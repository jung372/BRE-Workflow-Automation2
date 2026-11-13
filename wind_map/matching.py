"""Names alone never establish a project match."""
import re
import unicodedata
from .core import WindError

def normalized(value):
    return re.sub(r"[\s·,()㈜]", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()

def match_project(candidate, projects):
    matches = []
    for project in projects:
        if candidate.get("authority") and candidate.get("permit_number") and (candidate["authority"], candidate["permit_number"]) in [tuple(x) for x in project.get("permit_keys", [])]:
            matches.append((project["id"], ["permit_key"])); continue
        if candidate.get("business_code") and candidate["business_code"] in project.get("business_codes", []):
            matches.append((project["id"], ["business_code"])); continue
        clues = []
        for key in ("location", "operator"):
            if candidate.get(key) and normalized(candidate[key]) == normalized(project.get(key)):
                clues.append(key)
        if candidate.get("capacity_w") is not None and candidate["capacity_w"] == project.get("capacity_w"):
            clues.append("capacity_w")
        # A known location conflict defeats a same-operator/same-capacity match.
        conflict = candidate.get("location") and project.get("location") and normalized(candidate["location"]) != normalized(project["location"])
        if len(clues) >= 2 and not conflict:
            matches.append((project["id"], clues))
    if len(matches) == 1:
        return {"state": "candidate", "project_id": matches[0][0], "clues": matches[0][1]}
    return {"state": "review", "project_id": None, "candidates": [p for p, _ in matches], "reason": "ambiguous" if matches else "insufficient_independent_clues"}