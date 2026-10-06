"""Evidence-bound agreement grouping before the summary budget is consumed.

Headline similarity alone is never an event identity. A dated, completed,
quoted agreement with the same parties and purpose can corroborate another
report even when discovery supplied no project/entity metadata.
"""
from datetime import timedelta
import re


RULE_VERSION = "agreement-evidence-v2"
_ORGANIZATION = re.compile(r"[가-힣A-Za-z0-9·]{2,30}(?:발전|개발공사|건설|에너지|공사|중공업)")
_QUOTED = re.compile(r"[‘'“\"]([^‘'’“”\"\n]{12,180}(?:업무협약|양해각서)(?:\s*\(MOU\))?)[’'”\"]", re.I)
_DATE = re.compile(r"(?:(\d{4})년\s*)?(?:(\d{1,2})월\s*)?(\d{1,2})일")
_OTHER_STAGE = re.compile(r"본계약|공급계약|EPC.{0,12}(?:체결|계약)|금융\s*(?:약정|종결)|대출\s*(?:실행|약정)|조건부|해지|취소|변경", re.I)


def company(value):
    value = re.sub(r"㈜|주식회사|\(주\)|\s+", "", value)
    # Conventional short name, not a general removal of '한국' from entities.
    return value[2:] if value.startswith("한국") and value.endswith("발전") else value


def _grams(value):
    value = re.sub(r"[^가-힣a-z0-9]", "", value.lower()).replace("풍력사업", "풍력")
    return {value[i:i + 3] for i in range(len(value) - 2)}


def _event_date(prefix, published):
    matches = list(_DATE.finditer(prefix))
    if not matches:
        return None
    match = matches[-1]
    year, month, day = (int(x) if x else None for x in match.groups())
    # Resolve '지난 2일' against the publisher's KST date, including month/year
    # rollover. Publication date itself is never substituted for event date.
    for offset in range(32):
        candidate = published - timedelta(days=offset)
        if candidate.day == day and (month is None or candidate.month == month) and (year is None or candidate.year == year):
            return candidate.isoformat()
    return None


def agreement_anchor(article):
    title = article["title"]
    if _OTHER_STAGE.search(title) or re.search(r"사고|민원|소송|갈등|반대|예정|검토", title):
        return None
    body = article.get("text") or article.get("description", "")
    if not body:
        return None
    from .pipeline import KST, instant
    published = instant(article["source_published_at"]).astimezone(KST).date()
    found = []
    for quote in _QUOTED.finditer(body[:2500]):
        # Only the actual signing clause supplies actors/date/purpose. Later
        # EPC responsibilities or a historical project must not define its stage.
        boundary = body.rfind(". ", 0, quote.start())
        start = max(boundary + 2 if boundary >= 0 else 0, quote.start() - 240)
        prefix = body[start:quote.start()]
        suffix = body[quote.end():quote.end() + 55].split(". ")[0]
        if not re.search(r"체결(?:했|하였)", suffix):
            continue
        if re.search(r"예정|계획|추진|않|못|무산|불발|취소|해지", suffix) or _OTHER_STAGE.search(prefix):
            continue
        actors = sorted({company(x) for x in _ORGANIZATION.findall(prefix)})
        if len(actors) != 2 or not all(actor in company(title) for actor in actors):
            continue
        event_date = _event_date(prefix, published)
        purpose = quote.group(1)
        wind_type = next((x for x in ("육상풍력", "해상풍력") if x in purpose), None)
        if not event_date or not wind_type:
            continue
        scope = re.sub(r"[^가-힣a-z0-9]", "", purpose.lower().split(wind_type)[0]).removesuffix("지역")
        found.append({"actors": actors, "date": event_date, "stage": "MOU",
                      "purpose": purpose, "wind_type": wind_type, "scope": scope})
    # Ambiguous multiple signing events need review, not automatic grouping.
    unique = {(tuple(x["actors"]), x["date"], x["purpose"]): x for x in found}
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def same_agreement(left, right):
    a, b = left.get("agreement_anchor"), right.get("agreement_anchor")
    if not a or not b or any(a[k] != b[k] for k in ("actors", "date", "stage", "wind_type")):
        return False
    if left.get("project_name") and right.get("project_name") and left["project_name"] != right["project_name"]:
        return False
    if a["scope"] and b["scope"] and a["scope"] != b["scope"]:
        return False
    numbers = [set(re.findall(r"\d+(?:\.\d+)?", x["purpose"])) for x in (a, b)]
    if all(numbers) and numbers[0] != numbers[1]:
        return False
    x, y = _grams(a["purpose"]), _grams(b["purpose"])
    # Require corroborated distinctive agreement wording, not the generic word
    # '협약'. Use both directions so a short generic title cannot bridge events.
    shared = len(x & y)
    return shared >= 12 and shared / max(1, min(len(x), len(y))) >= .75 and shared / max(1, max(len(x), len(y))) >= .55


def agreement_classification(anchor):
    return {"contract_stage": "MOU", "stage_conflict": False,
            "primary_category": "MOU·협약", "review_required": False}


def group_events(articles):
    """Keep exact event identities; augment with complete-link corroboration."""
    groups, buckets = {}, {}
    for article in sorted(articles, key=lambda a: (a["source_published_at"], a["url"])):
        anchor = agreement_anchor(article)
        if anchor:
            article["agreement_anchor"] = anchor
            article.update(agreement_classification(anchor))
        existing = groups.get(article["event_id"])
        if existing is not None:
            existing.append(article)
            continue
        identity = (tuple(anchor["actors"]), anchor["date"], anchor["wind_type"]) if anchor else None
        possible = buckets.get(identity, []) if anchor else []
        match = next((members for members in possible
                      if all(same_agreement(article, other) for other in members)), None)
        if match is not None:
            article.update(event_id=match[0]["event_id"], event_key=match[0]["event_key"])
            match.append(article)
        else:
            groups[article["event_id"]] = [article]
            if anchor:
                buckets.setdefault(identity, []).append(groups[article["event_id"]])
    return groups
