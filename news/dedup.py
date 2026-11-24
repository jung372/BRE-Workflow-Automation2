"""Group corroborated reports of one event before consuming summary budget."""
from datetime import date, timedelta
from decimal import Decimal
import re
import unicodedata


RULE_VERSION = "news-event-evidence-v4"
_ORGANIZATION = re.compile(r"[가-힣A-Za-z0-9·]{2,30}(?:발전|개발공사|건설|에너지|공사|중공업)")
_QUOTED = re.compile(r"[‘'“\"]([^‘'’“”\"\n]{12,180}(?:업무협약|양해각서)(?:\s*\(MOU\))?)[’'”\"]", re.I)
_DATE = re.compile(r"(?:(\d{4})년\s*)?(?:(\d{1,2})월\s*)?(\d{1,2})일")
_OTHER_STAGE = re.compile(r"본계약|공급계약|EPC.{0,12}(?:체결|계약)|금융\s*(?:약정|종결)|대출\s*(?:실행|약정)|조건부|해지|취소|변경", re.I)
_ACTOR = re.compile(r"[가-힣A-Za-z0-9]{2,30}(?:발전|개발공사|건설|에너지|전력|중공업|그린파워|윈즈|솔라)")
_REGIONS = re.compile(r"서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|충청북도|충청남도|전라남도|전라북도|경상북도|경상남도")
_REGION_ALIASES = dict(zip(("충청북도", "충청남도", "전라북도", "전라남도", "경상북도", "경상남도"), ("충북", "충남", "전북", "전남", "경북", "경남")))
_CAPACITY = re.compile(r"(?<![\d.])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(gw|mw|kw)(?![a-z])", re.I)
_KINDS = [
    ("cancellation", r"해지|취소|철회"), ("accident", r"사고|사망|부상|화재"),
    ("dispute", r"소송|주민\s*반대|분쟁|갈등"),
    ("financial_close", r"금융\s*종결|PF\s*종결|financial close"),
    ("disbursement", r"대출.{0,6}실행|자금.{0,6}집행"),
    ("financing", r"금융\s*약정|대출\s*약정"),
    ("final_contract", r"본계약|EPC.{0,10}(?:체결|계약)|공급계약|PPA.{0,10}(?:체결|계약)"),
    ("operation", r"상업\s*운전|상업\s*가동"), ("completion", r"준공"),
    ("construction", r"착공"), ("permit", r"허가\s*(?:취득|획득|승인)|허가받|인허가\s*승인"),
    ("tender", r"입찰\s*(?:선정|낙찰|탈락)|낙찰|우선협상"),
    ("agreement", r"MOU|양해각서|업무협약|협약\s*체결"),
    ("cooperation", r"간담회|기술\s*교류회|협력|논의|협의|협업"),
]


def _normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).lower()


# The organizer/venue and subject identify the event even when one outlet
# calls it a forum and another calls it a discussion, or leads with a policy.
_NAMED_MEETINGS = (
    ("floating-wind-hydrogen-forum",
     re.compile(r"(?:hd|에이치디)현대중공업(?:\s*영빈관|에서|서)"),
     re.compile(r"해상\s*풍력.{0,12}(?:친환경[ (]*그린[ )]*|그린)\s*수소.{0,22}협력.{0,9}(?:포럼|토론회)")),
    ("marine-plant-services-industry-event",
     re.compile(r"해양수산부|해수부|선박해양플랜트연구소"),
     re.compile(r"해양\s*플랜트\s*서비스\s*산업\s*산\s*[·ㆍ・]\s*학\s*[·ㆍ・]\s*연\s*협력\s*행사")),
)


def _named_meeting(article):
    from .pipeline import KST, instant

    published = instant(article["source_published_at"]).astimezone(KST).date()
    for value in (article.get("description"), article.get("text"), article["title"]):
        lead = _normalized(value)[:1500]
        for name, organizer, topic in _NAMED_MEETINGS:
            match = topic.search(lead)
            if not match:
                continue
            context = lead[max(0, match.start() - 160):match.end() + 45]
            if not organizer.search(context) or re.search(r"취소|무산|불발|열지 않|열리지 않|개최하지 않", context):
                continue
            prefix = lead[max(0, match.start() - 110):match.start()]
            # A scheduled meeting can be reported one day before it takes place.
            meeting_day = _event_date(prefix, published + timedelta(days=1))
            if meeting_day and abs((published - date.fromisoformat(meeting_day)).days) > 3:
                continue
            return name, meeting_day, published.isoformat()
    return None


def _entity(value):
    return re.sub(r"\([^)]*\)", "", company(_normalized(value))).strip("·.,()")


def _project(value):
    value = _normalized(value)
    value = re.sub(r"부유식|고정식|해상\s*풍력|육상\s*풍력|풍력|태양광|발전소|공동\s*개발|프로젝트|사업", "", value)
    return re.sub(r"[^가-힣a-z0-9]", "", value)


def event_anchor(article, industry="wind"):
    """Use the subject and lead, not historical stages deep in the article.

    Unknown facts are left empty. In particular, a publication/announcement
    date is not evidence that the meeting or contract happened on that date.
    """
    title = _normalized(article["title"])
    lead = _normalized(article.get("text") or article.get("description", ""))[:900]
    first_sentence = re.split(r"(?<=[다요])\.\s+", lead, maxsplit=1)[0]
    subject = title + " " + lead
    kind, clause = "", ""
    for section in (title, first_sentence):
        found = next(((name, match) for name, pattern in _KINDS
                      if (match := re.search(pattern, section, re.I))), None)
        if found:
            kind, match = found
            clause = section[:match.start()]
            break
    # If the headline supplies the kind but no date, inspect the corresponding
    # lead clause. Dates after the action ('13일 밝혔다') are not event dates.
    if kind and not _DATE.search(clause):
        pattern = dict(_KINDS)[kind]
        match = re.search(pattern, first_sentence, re.I)
        if match:
            clause = first_sentence[:match.start()]
    status = "planned" if re.search(r"예정|계획이다|추진할", title + " " + first_sentence) else "actual"
    if re.search(r"체결하지|개최하지|않았다|못했다|무산|불발", title + " " + first_sentence):
        status = "negated"
    event_day = article.get("event_date")
    announcement = (re.search(r"밝혔다|전했다|알렸다", first_sentence)
                    and not re.search(r"지난\s*(?:\d+년\s*)?(?:\d+월\s*)?\d+일", clause))
    if not event_day and _DATE.search(clause) and not announcement:
        from .pipeline import KST, instant
        event_day = _event_date(clause, instant(article["source_published_at"]).astimezone(KST).date())
    actors = {_entity(x) for x in _ACTOR.findall(subject)}
    # Pipeline ingestion validates supplied entities against the full evidence.
    actors.update(_entity(x) for x in article.get("companies", []))
    actors.difference_update({"재생에너지", "신재생에너지", "해상발전", "태양광발전", "풍력발전"})
    regions = set()
    for section in (title, lead, _normalized(article.get("region"))):
        regions = {_REGION_ALIASES.get(x, x) for x in _REGIONS.findall(section)}
        if regions:
            break
    capacities = {str(Decimal(n.replace(",", "")) * {"gw": 1000, "mw": 1, "kw": Decimal("0.001")}[u.lower()])
                  for n, u in _CAPACITY.findall(title + " " + first_sentence)}
    capacities = {str(Decimal(n).normalize()) for n in capacities}
    projects = set()
    supplied = article.get("project_name")
    if supplied and _project(supplied):
        projects.add(_project(supplied))
    project_matches = list(re.finditer(r"([가-힣a-z0-9]+)[’'”\"]?\s+(?:(?:부유식|고정식|수상형?|영농형|지붕형?|지상형|건물일체형)\s*)?(?:해상\s*풍력|육상\s*풍력|태양광)(?:\s|$)", subject))
    project_matches += list(re.finditer(r"([가-힣a-z][가-힣a-z0-9]{2,})[’'”\"]?\s*(?:사업|프로젝트)", subject))
    for match in project_matches:
        name = _project(match.group(1))
        if (name and name not in {"규모", "규모의", "위한", "국내", "글로벌", "추진중인", "수상", "영농형", "지붕형"}
                and not name.endswith(("들과", "들은", "위해", "하고", "하며", "위한", "중인", "있는", "로", "과", "와", "는", "은", "을", "를"))
                and not _REGIONS.fullmatch(name) and not _CAPACITY.fullmatch(name)):
            projects.add(name)
    types = ("부유식", "육상", "고정식") if industry == "wind" else ("영농형", "수상", "지붕", "건물일체형", "지상형")
    subtype = next((x for section in (title, lead) for x in types if x in section), "")
    return {"meeting": _named_meeting(article), "actors": actors, "regions": regions, "capacities": capacities, "projects": projects,
            "kind": kind, "status": status, "date": event_day, "subtype": subtype,
            "grams": _grams(subject), "title_grams": _grams(title)}


def same_event(left, right, industry="wind"):
    """Require shared event evidence; a common company or topic is insufficient."""
    a_agreement, b_agreement = left.get("agreement_anchor"), right.get("agreement_anchor")
    if a_agreement and b_agreement:
        return same_agreement(left, right)
    a = left.get("news_anchor") or event_anchor(left, industry)
    b = right.get("news_anchor") or event_anchor(right, industry)
    meeting_a, meeting_b = a["meeting"], b["meeting"]
    if meeting_a and meeting_b:
        name_a, day_a, published_a = meeting_a
        name_b, day_b, published_b = meeting_b
        return (name_a == name_b and "negated" not in (a["status"], b["status"])
                and (not day_a or not day_b or day_a == day_b)
                and abs((date.fromisoformat(published_a) - date.fromisoformat(published_b)).days) <= 2)
    if a["status"] != b["status"]:
        return False
    for field in ("kind", "date", "subtype"):
        if a[field] and b[field] and a[field] != b[field]:
            return False
    for field in ("regions", "capacities", "projects"):
        if a[field] and b[field] and not a[field] & b[field]:
            return False
    actors = a["actors"] & b["actors"]
    if (len(a["actors"]) >= 2 and len(b["actors"]) >= 2
            and not (a["actors"] <= b["actors"] or b["actors"] <= a["actors"])):
        return False
    project = bool(a["projects"] & b["projects"])
    location = bool(a["regions"] & b["regions"])
    capacity = bool(a["capacities"] & b["capacities"])
    kind = bool(a["kind"] and a["kind"] == b["kind"])
    shared = len(a["grams"] & b["grams"])
    similarity = shared / max(1, min(len(a["grams"]), len(b["grams"])))
    matching_subject = (a["subtype"] and a["subtype"] == b["subtype"]
                        and len(a["title_grams"] & b["title_grams"]) >= 7)
    if len(actors) >= 2 and (project or location or capacity or matching_subject) and kind:
        return True
    if actors and project and kind:
        return True
    if actors and capacity and kind and len(a["title_grams"] & b["title_grams"]) >= 8:
        return True
    # A sparse report may omit the project name or capacity. Still require the
    # same actor, location, technology, action and substantial headline wording.
    if (actors and location and kind and a["subtype"] and a["subtype"] == b["subtype"]
            and len(a["title_grams"] & b["title_grams"]) >= 7):
        return True
    return bool((actors or project) and shared >= 30 and similarity >= .8
                and len(a["title_grams"] & b["title_grams"]) / max(1, min(len(a["title_grams"]), len(b["title_grams"]))) >= .45)


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


def agreement_anchor(article, industry="wind"):
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
        types = ("육상풍력", "해상풍력") if industry == "wind" else ("영농형 태양광", "수상 태양광", "지붕 태양광", "태양광")
        wind_type = next((x for x in types if x in purpose), None)
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


def group_events(articles, industry="wind"):
    """Keep exact event identities; augment with complete-link corroboration."""
    groups, buckets = {}, {}
    for article in sorted(articles, key=lambda a: (a["source_published_at"], a["url"])):
        article.pop("agreement_anchor", None)
        anchor = agreement_anchor(article, industry)
        if anchor:
            article["agreement_anchor"] = anchor
            article.update(agreement_classification(anchor))
        article["news_anchor"] = event_anchor(article, industry)
        existing = groups.get(article["event_id"])
        if existing is not None:
            existing.append(article)
            continue
        keys = ([("agreement", tuple(anchor["actors"]), anchor["date"], anchor["wind_type"])] if anchor else
                [("actor", x) for x in sorted(article["news_anchor"]["actors"])] +
                [("project", x) for x in sorted(article["news_anchor"]["projects"])])
        meeting = article["news_anchor"]["meeting"]
        if meeting:
            keys.append(("meeting", meeting[0]))
        possible = dict.fromkeys(event_id for key in keys for event_id in buckets.get(key, []))
        match = next((groups[event_id] for event_id in possible
                      if all(same_event(article, other, industry) for other in groups[event_id])), None)
        if match is not None:
            article.update(event_id=match[0]["event_id"], event_key=match[0]["event_key"])
            match.append(article)
        else:
            groups[article["event_id"]] = [article]
        for key in keys:
            bucket = buckets.setdefault(key, [])
            if article["event_id"] not in bucket:
                bucket.append(article["event_id"])
    return groups
