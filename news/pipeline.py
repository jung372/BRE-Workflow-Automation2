"""Deterministic normalization, evidence boundaries, frozen issue revisions."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import html
import json
import re
import time as monotonic_time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import polars as pl

from .store import Conflict, canonical, digest, utcnow
from .relevance import industry_relevant, editorial_order

KST = timezone(timedelta(hours=9))
SUCCESS = {"success", "success_zero"}
PUBLIC_ITEM_FIELDS = {"event_id", "representative_article_id", "headline", "summary", "primary_category", "tags", "source_name", "source_url", "source_published_at", "timestamp_basis", "evidence_scope", "project_name", "companies", "region", "wind_type", "contract_stage", "late_arrival", "amount", "currency", "capacity", "capacity_unit", "event_date"}
EDITABLE_FIELDS = {"headline", "summary", "primary_category", "tags", "project_name", "companies", "region", "wind_type", "contract_stage"}


def instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(timezone.utc)


def window(issue_date, cutoff=None):
    day = date.fromisoformat(issue_date)
    end = datetime.combine(day, time(7, 30), tzinfo=KST)
    if cutoff is not None and instant(cutoff) != end.astimezone(timezone.utc):
        raise ValueError("cutoff_must_be_issue_date_0730_kst")
    return end - timedelta(days=1), end


def normalize_url(value, hosts):
    parts = urlsplit(str(value))
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443) or host not in hosts or "\\" in str(value) or any(ord(c) < 32 for c in str(value)):
        raise ValueError("unsafe_source_url")
    # Article query identifiers are preserved; only known tracking keys disappear.
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit(("https", host, parts.path or "/", urlencode(params), ""))


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()


def free_access(article):
    """A missing paywall flag is not evidence that an article is free to read."""
    return article.get("access_status") == "free" and article.get("is_paywalled", False) is False


def classify(article):
    text = article["title"] + " " + article.get("description", "") + " " + article.get("text", "")
    stages = [("해지·변경", r"계약(?:을|이|의)?\s*(?:해지|변경)|(?:해지|변경)\s*(?:된|한)?\s*계약"), ("조건부 계약", r"조건부.{0,8}계약"), ("금융종결", r"금융종결|금융 종결|PF\s*종결|financial close"), ("실행", r"대출.{0,6}실행|자금.{0,6}집행"), ("금융약정", r"금융약정|금융 약정|대출 약정|대출약정"), ("MOU", r"MOU|양해각서|업무협약"), ("우선협상", r"우선협상"), ("본계약", r"본계약|EPC.{0,10}(체결|계약)|공급계약.{0,5}체결"), ("검토", r"검토|추진 예정")]
    matches = [name for name, pattern in stages if re.search(pattern, text, re.I)]
    # Multiple explicit stages can mean a historical comparison. Human review resolves it.
    stage = matches[0] if len(matches) == 1 else "미확인"
    categories = [("사고·안전", r"사고|사망|부상|화재|안전사고"), ("민원·수용성", r"민원|주민 반대|분쟁|소송|갈등"), ("금융·PF", r"금융|PF|대출|자금조달"), ("EPC·시공", r"EPC|시공"), ("MOU·협약", r"MOU|양해각서|업무협약"), ("터빈·공급계약", r"터빈|공급계약"), ("인허가·정책", r"인허가|허가|정책|법안|입찰"), ("착공·준공·상업운전", r"착공|준공|상업운전"), ("산업·공급망", r"공급망|해저케이블|하부구조|설치선|O&M")]
    # Full public articles contain historical/incidental accident or complaint
    # references. Classify the subject from the headline first, then the lead.
    headline_categories = [("민원·수용성", r"어민|상생|공존|수용성|보상"),
                           ("인허가·정책", r"전력망|계통|예측|풍황|RPS|입찰")]
    category = next((name for name, pattern in categories + headline_categories
                     if re.search(pattern, article["title"], re.I)), None)
    if category is None:
        lead = article.get("description") or article.get("text", "")[:400]
        category = next((name for name, pattern in categories if re.search(pattern, lead, re.I)), "기타 주요 동향")
    wind_type = "부유식 해상" if "부유식" in text else "고정식 해상" if "해상풍력" in text else "육상" if "육상풍력" in text else "공통"
    return {"contract_stage": stage, "primary_category": category, "wind_type": wind_type, "review_required": len(matches) > 1 or category in {"사고·안전", "민원·수용성"}, "stage_conflict": len(matches) > 1}


def event_key(article, classification):
    # Missing identity never causes fuzzy/transitive merging. Supplied project/entity
    # metadata is trusted only after its text occurrence has been checked at ingest.
    identity = (article.get("project_name"), article.get("region"), tuple(sorted(article.get("companies", []))), article.get("event_date"))
    if not all(identity) or classification["contract_stage"] == "미확인":
        # Only exact normalized syndicated content can merge without identity.
        # Distinct stages remain different even when a project is the same.
        return digest({"headline": article["title"], "evidence": article.get("description", "") or article.get("text", ""), "stage": classification["contract_stage"], "category": classification["primary_category"]})
    return digest({"identity": identity, "stage": classification["contract_stage"], "category": classification["primary_category"]})


def automatic_allowed(policy):
    if policy.get("review_mode") == "codex_oauth":
        return bool(policy.get("automatic_publication"))
    gate = policy.get("quality_gate", {})
    return bool(policy.get("automatic_publication") and gate.get("approved") and gate.get("private_days", 0) >= 7 and gate.get("evaluated_article_pairs", 0) >= 200 and gate.get("evaluated_events", 0) >= 50 and gate.get("verified_representatives", 0) >= 50 and gate.get("merge_precision", 0) >= .95 and gate.get("merge_recall", 0) >= .90 and gate.get("stage_merge_errors", 1) == 0 and gate.get("fact_match_rate", 0) == 1)


def approval_digest(public, candidates, batch_ids):
    # The transport publication time is assigned after approval. All editorial
    # content, evidence/member decisions and frozen batches remain hash-bound.
    return digest({"issue": {k: v for k, v in public.items() if k != "published_at"}, "candidates": candidates, "batch_ids": batch_ids})


class Pipeline:
    def __init__(self, store, collection, policy, summarizer=None):
        self.store, self.collection, self.policy = store, collection, policy
        if summarizer is None:
            from .summarizer import summarize
            summarizer = summarize
        self.summarizer = summarizer

    def ingest(self, payload, batch_id):
        raw = payload.get("articles", [])
        results = payload.get("source_results", [])
        if not isinstance(raw, list) or len(raw) > 10000 or not isinstance(results, list):
            raise ValueError("invalid_batch")
        sources = {s["source_id"]: s for s in self.collection.get("sources", [])}
        if len({r.get("source_id") for r in results}) != len(results):
            raise ValueError("duplicate_source_result")
        allowed_status = SUCCESS | {"disabled", "auth_error", "request_error", "http_error", "parse_error", "partial", "saturated"}
        if any(r.get("source_id") not in sources or r.get("status") not in allowed_status or not isinstance(r.get("count"), int) or r["count"] < 0 for r in results):
            raise ValueError("invalid_source_result")
        normalized, rejected, excluded = [], [], 0
        for entry in raw:
            try:
                source = sources[entry["source_id"]]
                if not source.get("enabled", True) or not source.get("domestic") or source.get("language") != "ko":
                    raise ValueError("ineligible_source")
                if self.policy.get("require_free_access", False) and not free_access(entry):
                    excluded += 1
                    continue
                if source.get("discovery") == "naver_news_search":
                    from .search_sources import publisher_url
                    url = publisher_url(entry["url"])
                else:
                    url = normalize_url(entry["url"], source.get("hosts", source.get("allowed_hosts", [])))
                title, description = clean(entry["title"]), clean(entry.get("description"))
                text = clean(entry.get("text")) if source.get("allow_body", False) else ""
                if not title:
                    raise ValueError("missing_title")
                if not re.search(r"[가-힣]", title) or not industry_relevant(title, description + " " + text):
                    excluded += 1
                    continue
                published = instant(entry["source_published_at"])
                combined = title + " " + description + " " + text
                companies = entry.get("companies", [])
                if not isinstance(companies, list) or any(not isinstance(c, str) or not c.strip() or c not in combined for c in companies):
                    raise ValueError("unsupported_entity")
                project = entry.get("project_name")
                region = entry.get("region")
                event_date = entry.get("event_date")
                if project and project not in combined or region and region not in combined:
                    raise ValueError("unsupported_entity")
                if event_date and event_date not in combined:
                    event_date = None
                article_id = hashlib.sha256(url.encode()).hexdigest()
                normalized.append({"article_id": article_id, "url": url, "title": title, "description": description, "text": text, "source_id": source["source_id"], "source_name": source.get("name", source["source_id"]), "source_published_at": published.isoformat(), "timestamp_basis": entry.get("timestamp_basis", entry.get("published_at_basis", "source")), "evidence_scope": "full_text" if text else "description" if description else "title", "project_name": project, "companies": companies, "region": region, "event_date": event_date, "trust_score": source.get("trust_score", 0)})
                normalized[-1]["access_status"] = "free" if free_access(entry) else "paid" if entry.get("access_status") == "paid" else "unknown"
                if source.get("discovery") == "naver_news_search":
                    normalized[-1]["source_name"] = clean(entry.get("source_name"))[:100] or urlsplit(url).hostname
                normalized[-1]["access_basis"] = clean(entry.get("access_basis", ""))[:80]
                normalized[-1]["access_checked_at"] = instant(entry["access_checked_at"]).isoformat() if entry.get("access_checked_at") else None
            except (KeyError, ValueError, TypeError, OverflowError):
                rejected.append(entry.get("source_id") if isinstance(entry, dict) else None)
        # Polars performs vectorized whitespace cleanup and exact content dedup.
        if normalized:
            frame = pl.DataFrame(normalized, strict=False).with_columns(pl.col("title").str.replace_all(r"\s+", " ").str.strip_chars(), pl.col("description").str.replace_all(r"\s+", " ").str.strip_chars())
            normalized = frame.unique(subset=["url"], keep="last", maintain_order=True).to_dicts()
        safe_results = []
        for result in results:
            safe = {"source_id": result["source_id"], "status": result["status"], "count": result["count"]}
            if result.get("error_code"):
                safe["error_code"] = re.sub(r"[^A-Z0-9_]", "", str(result["error_code"]).upper())[:80]
            for field in ("discovered", "attempted", "excluded_restricted"):
                if isinstance(result.get(field), int) and 0 <= result[field] <= 10000:
                    safe[field] = result[field]
            # Structured counts only; never save search snippets or raw errors.
            for field in ("query_results", "publisher_results", "outcomes"):
                if isinstance(result.get(field), dict):
                    value = result[field]
                    if len(canonical(value)) <= 50000: safe[field] = value
            if result["source_id"] in rejected and result["status"] in SUCCESS:
                safe.update(status="parse_error", error_code="NORMALIZATION_FAILED")
            safe_results.append(safe)
        now = utcnow()
        def work(db):
            if db.execute("SELECT 1 FROM batches WHERE batch_id=?", [batch_id]).fetchone():
                return {"batch_id": batch_id, "ingested": len(normalized), "rejected": len(rejected)}
            db.execute("INSERT INTO batches VALUES (?,?,?)", [batch_id, now, canonical(safe_results)])
            for article in normalized:
                content_hash = digest({"title": article["title"], "description": article["description"], "text": article["text"]})
                metadata = {k: v for k, v in article.items() if k not in {"description", "text"}}
                evidence = {"description": article["description"], "text": article["text"], "captured_at": now, "content_hash": content_hash}
                db.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?) ON CONFLICT(article_id) DO UPDATE SET content_hash=excluded.content_hash,metadata=excluded.metadata,evidence=excluded.evidence,updated_at=excluded.updated_at", [article["article_id"], article["url"], content_hash, canonical(metadata), canonical(evidence), now, now])
                db.execute("INSERT INTO batch_articles VALUES (?,?,?) ON CONFLICT DO NOTHING", [batch_id, article["article_id"], canonical({**article, "content_hash": content_hash, "discovered_at": now})])
            return {"batch_id": batch_id, "ingested": len(normalized), "rejected": len(rejected), "excluded": excluded,
                    "source_results": safe_results, "publisher_counts": dict(pl.DataFrame(normalized).group_by("source_name").len().iter_rows()) if normalized else {}}
        return self.store.call(work, write=True)

    def prepare(self, payload):
        day = payload["issue_date"]
        start, end = window(day, payload.get("cutoff"))
        issue_id = "wind-" + day
        previous = self.store.issue(issue_id)
        correction = bool(payload.get("correction_reason"))
        if previous and not correction:
            if previous["state"] != "BLOCKED" or not payload.get("retry_blocked"):
                return previous
        if correction and (not previous or previous["state"] not in {"COMMITTED", "WEB_VERIFIED"}):
            raise Conflict("correction_requires_published_issue")
        revision = previous["revision"] + 1 if previous else 1
        def read(db):
            batches = db.execute("SELECT batch_id,created_at,source_results FROM batches ORDER BY created_at,batch_id").fetchall()
            selected = payload.get("batch_ids")
            if selected is not None:
                if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected):
                    raise ValueError("invalid_batch_ids")
                available = {b[0] for b in batches}
                if any(b not in available for b in selected):
                    raise ValueError("batch_not_found")
                batches = [b for b in batches if b[0] in selected]
            else:
                # A stale previous-day healthy batch cannot mask a failed cutoff run.
                next_midnight = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(), tzinfo=KST)
                batches = [b for b in batches if start.astimezone(timezone.utc) <= instant(b[1]) < next_midnight.astimezone(timezone.utc)]
            frozen_ids = [b[0] for b in batches]
            articles = []
            if frozen_ids:
                placeholders = ",".join("?" for _ in frozen_ids)
                by_article = {}
                for row in db.execute("SELECT a.article_id,a.metadata,a.evidence,a.discovered_at,a.content_hash,b.article_snapshot FROM articles a JOIN batch_articles b ON a.article_id=b.article_id JOIN batches z ON b.batch_id=z.batch_id WHERE b.batch_id IN (" + placeholders + ") ORDER BY z.created_at,z.batch_id", frozen_ids).fetchall():
                    article = json.loads(row[5]) if row[5] else {**json.loads(row[1]), **json.loads(row[2]), "discovered_at": row[3], "content_hash": row[4]}
                    by_article[row[0]] = article
                articles = list(by_article.values())
            published_articles, published_events = set(), set()
            for row in db.execute("SELECT payload,candidates FROM issues WHERE state IN ('COMMITTED','WEB_VERIFIED') AND issue_id<>?", [issue_id]).fetchall():
                for item in json.loads(row[0])["items"]:
                    published_articles.add(item["representative_article_id"])
                    published_events.add(item["event_id"])
                selected_events = {item["event_id"] for item in json.loads(row[0])["items"]}
                for candidate in json.loads(row[1]):
                    if candidate["item"]["event_id"] in selected_events:
                        published_articles.update(candidate["member_article_ids"])
            return batches, frozen_ids, articles, published_articles, published_events
        batches, frozen_ids, articles, prior_articles, prior_events = self.store.call(read)
        source_status = {}
        for batch in batches:
            for result in json.loads(batch[2]):
                source_status[result["source_id"]] = result
        active = [s for s in self.collection.get("sources", []) if s.get("enabled", True)]
        successes = sum(source_status.get(s["source_id"], {}).get("status") in SUCCESS for s in active)
        required_failed = not active or not any(s.get("required") for s in active) or any(source_status.get(s["source_id"], {}).get("status") not in SUCCESS for s in active if s.get("required"))
        # Date-only prepare requires a completed cutoff collection. Explicit
        # batch selection supports reviewed backfills without fabricating times.
        if payload.get("batch_ids") is None and not any(instant(b[1]) >= end.astimezone(timezone.utc) for b in batches):
            required_failed = True
        coverage = {"expected_sources": len(active), "successful_sources": successes, "partial": successes < len(active), "source_results": list(source_status.values())}
        candidates, groups, eligible = [], {}, 0
        for article in articles:
            if self.policy.get("require_free_access", False) and not free_access(article):
                continue
            published = instant(article["source_published_at"])
            if not end.astimezone(timezone.utc) - timedelta(hours=72) <= published < end.astimezone(timezone.utc) or article["article_id"] in prior_articles:
                continue
            eligible += 1
            classification = classify(article)
            key = event_key(article, classification)
            event_id = "evt-" + key[:24]
            if event_id in prior_events:
                continue
            article.update(classification, event_id=event_id, event_key=key, late_arrival=published < start.astimezone(timezone.utc))
            groups.setdefault(event_id, []).append(article)
        summary_start = monotonic_time.monotonic()
        max_summaries = min(30, max(1, int(self.policy.get("max_summary_candidates", 20))))
        summary_deadline = min(900, max(1, int(self.policy.get("summary_deadline_seconds", 900))))
        for candidate_number, (event_id, members) in enumerate(editorial_order(groups)):
            members.sort(key=lambda a: (-a["trust_score"], -len(a.get("text", "")), -len(a.get("description", "")), a["source_published_at"], a["url"]))
            article = members[0]
            if candidate_number >= max_summaries or monotonic_time.monotonic() - summary_start >= summary_deadline:
                summary = {"summary": "", "valid": False, "review_required": True, "validation_status": "SUMMARY_BUDGET_EXHAUSTED"}
            else:
                summary = self.summarizer(article, self.policy.get("summarizer", {}))
            item = {"event_id": event_id, "representative_article_id": article["article_id"], "headline": article["title"], "summary": summary.get("summary", ""), "primary_category": article["primary_category"], "tags": [article["contract_stage"]] if article["contract_stage"] != "미확인" else [], "source_name": article["source_name"], "source_url": article["url"], "source_published_at": article["source_published_at"], "timestamp_basis": article["timestamp_basis"], "evidence_scope": article["evidence_scope"], "project_name": article.get("project_name"), "companies": article.get("companies", []), "region": article.get("region"), "wind_type": article["wind_type"], "contract_stage": article["contract_stage"], "late_arrival": article["late_arrival"], "amount": None, "currency": None, "capacity": None, "capacity_unit": None}
            validated = summary.get("valid") is True and isinstance(summary.get("summary"), str) and bool(summary["summary"].strip())
            if validated and summary.get("validation_status") == "CODEX_VERIFIED":
                metadata = summary.get("metadata", {})
                combined = article["title"] + " " + article.get("description", "") + " " + article.get("text", "")
                if isinstance(metadata.get("companies"), list) and all(isinstance(c, str) and c and c in combined for c in metadata["companies"]):
                    item["companies"] = metadata["companies"]
                for field in ("project_name", "region"):
                    value = metadata.get(field)
                    if value is None or isinstance(value, str) and value and value in combined:
                        item[field] = value
            oauth_reviewed = self.policy.get("review_mode") == "codex_oauth" and validated and summary.get("validation_status") == "CODEX_VERIFIED" and summary.get("review_required") is False
            sensitive = (article["review_required"] or summary.get("review_required", True)) and not oauth_reviewed
            rights = next((s.get("rights_reviewed", False) for s in active if s["source_id"] == article["source_id"]), False)
            held = not validated or not item["summary"] or (self.policy.get("require_rights_review", True) and not rights) or (self.policy.get("review_mode") == "codex_oauth" and not oauth_reviewed)
            diagnostic = str(summary.get("fallback_reason") or summary.get("validation_status") or "SUMMARY_VALIDATION_FAILED")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", diagnostic):
                diagnostic = "SUMMARY_VALIDATION_FAILED"
            reason = diagnostic if not validated or (self.policy.get("review_mode") == "codex_oauth" and not oauth_reviewed) else "SOURCE_RIGHTS_UNREVIEWED" if held else "editorial_review" if sensitive else "draft_mode"
            candidates.append({"item": item, "member_article_ids": [a["article_id"] for a in members], "evidence_hash": article["content_hash"], "review_required": sensitive, "held": held, "approved": automatic_allowed(self.policy) and not sensitive and not held, "reason": reason})
        max_items = min(15, max(0, int(self.policy.get("max_items", 15))))
        candidates.sort(key=lambda c: (c["item"]["primary_category"], c["item"]["source_published_at"], c["item"]["source_url"]))
        selected = [c["item"] for c in candidates if c["approved"]][:max_items]
        deferred_count = sum(not c["approved"] and c["reason"] == "SUMMARY_BUDGET_EXHAUSTED" for c in candidates)
        held_count = sum(not c["approved"] and c["reason"] != "SUMMARY_BUDGET_EXHAUSTED" for c in candidates)
        public = {"schema_version": 1, "issue_id": issue_id, "issue_date": day, "revision": revision, "timezone": "Asia/Seoul", "window_start": start.isoformat(), "window_end": end.isoformat(), "published_at": None, "content_status": "partial" if coverage["partial"] or held_count else "normal" if selected else "no_news", "coverage": coverage, "counts": {"fetched": len(articles), "eligible_articles": eligible, "merged_duplicates": sum(len(m) - 1 for m in groups.values()), "published_topics": len(selected), "held": held_count}, "headline_summary": [item["headline"] for item in selected[:3]], "items": selected}
        public['counts']['deferred'] = deferred_count
        if correction:
            public.update(correction_reason=clean(payload["correction_reason"]), corrected_at=utcnow())
        state = "BLOCKED" if required_failed else "READY" if automatic_allowed(self.policy) else "REVIEW_REQUIRED" if candidates else "DRAFT"
        content_hash = approval_digest(public, candidates, frozen_ids)
        approval_hash = content_hash if state == "READY" else None
        def write(db):
            now = utcnow()
            db.execute("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [issue_id, revision, day, state, content_hash, approval_hash, canonical(public), canonical(candidates), canonical(frozen_ids), None, now, now])
            for event_id, members in groups.items():
                db.execute("INSERT INTO events VALUES (?,?,?) ON CONFLICT DO NOTHING", [event_id, members[0]["event_key"], canonical({"rule_version": "conservative-v1", "stage": members[0]["contract_stage"], "representative_article_id": members[0]["article_id"]})])
                for member in members:
                    db.execute("INSERT INTO event_articles VALUES (?,?,?) ON CONFLICT DO NOTHING", [event_id, member["article_id"], "exact_identity_and_stage"])
        self.store.call(write, write=True)
        return self.store.issue(issue_id)

    def review(self, issue_id, request):
        def work(db):
            record = self.store._issue(db.execute("SELECT * FROM issues WHERE issue_id=? ORDER BY revision DESC LIMIT 1", [issue_id]).fetchone())
            if not record:
                raise ValueError("issue_not_found")
            if request.get("revision") != record["revision"] or request.get("approval_hash") != record["content_hash"]:
                raise Conflict("stale_approval_hash")
            if record["state"] in {"PUBLISHING", "COMMITTED", "WEB_VERIFIED", "BLOCKED"} or record["payload"].get("published_at") is not None:
                raise Conflict("issue_not_reviewable")
            candidates, public = record["candidates"], record["payload"]
            action = request.get("action")
            item_ids = request.get("item_ids")
            if item_ids is not None and (not isinstance(item_ids, list) or any(i not in {c["item"]["event_id"] for c in candidates} for i in item_ids)):
                raise ValueError("unknown_item_id")
            targets = [c for c in candidates if item_ids is None or c["item"]["event_id"] in item_ids]
            if action in {"approve", "hold", "exclude"}:
                for candidate in targets:
                    if action == "approve" and candidate["held"] and not request.get("reason"):
                        raise ValueError("held_override_requires_reason")
                    candidate.update(approved=action == "approve", reason=clean(request.get("reason", action)))
            elif action in {"merge", "split", "representative"}:
                if not request.get("reason"):
                    raise ValueError("editorial_reason_required")
                if action == "merge":
                    if len(targets) < 2:
                        raise ValueError("merge_requires_multiple_events")
                    for field in ("contract_stage", "primary_category", "project_name", "region"):
                        values = {c["item"].get(field) for c in targets if c["item"].get(field)}
                        if len(values) > 1:
                            raise ValueError("merge_identity_or_stage_conflict")
                    if targets[0]["item"]["contract_stage"] == "미확인":
                        raise ValueError("merge_stage_must_be_verified")
                    primary = targets[0]
                    primary["member_article_ids"] = sorted({a for c in targets for a in c["member_article_ids"]})
                    primary.update(approved=False, reason=clean(request["reason"]))
                    candidates = [c for c in candidates if c not in targets[1:]]
                    # One representative's facts are retained, never mixed.
                elif action == "representative":
                    if len(targets) != 1 or request.get("article_id") not in targets[0]["member_article_ids"]:
                        raise ValueError("representative_must_be_event_member")
                    self._replace_representative(db, targets[0], request["article_id"])
                    targets[0]["reason"] = clean(request["reason"])
                else:
                    if len(targets) != 1 or len(targets[0]["member_article_ids"]) < 2:
                        raise ValueError("split_requires_merged_event")
                    old = targets[0]
                    candidates.remove(old)
                    for article_id in old["member_article_ids"]:
                        candidate = json.loads(canonical(old))
                        candidate["member_article_ids"] = [article_id]
                        candidate["item"]["event_id"] = "evt-manual-" + digest({"issue": issue_id, "article": article_id})[:20]
                        self._replace_representative(db, candidate, article_id)
                        candidate["reason"] = clean(request["reason"])
                        candidates.append(candidate)
            elif action == "edit":
                if len(targets) != 1 or not request.get("reason"):
                    raise ValueError("edit_requires_one_item_and_reason")
                changes = request.get("changes", {})
                if not changes or not set(changes) <= EDITABLE_FIELDS:
                    raise ValueError("invalid_edit_fields")
                if "summary" in changes and (not isinstance(changes["summary"], str) or not changes["summary"].strip()):
                    raise ValueError("summary_string_required")
                if "companies" in changes and (not isinstance(changes["companies"], list) or any(not isinstance(c, str) for c in changes["companies"])):
                    raise ValueError("companies_array_required")
                targets[0]["item"].update(changes)
                targets[0].update(approved=False, reason=clean(request["reason"]))
            else:
                raise ValueError("invalid_review_action")
            selected = [c["item"] for c in candidates if c["approved"]][:min(15, self.policy.get("max_items", 15))]
            public["items"] = selected
            public["counts"].update(published_topics=len(selected),
                held=sum(not c["approved"] and c["reason"] != "SUMMARY_BUDGET_EXHAUSTED" for c in candidates),
                deferred=sum(not c["approved"] and c["reason"] == "SUMMARY_BUDGET_EXHAUSTED" for c in candidates))
            public["headline_summary"] = [i["headline"] for i in selected[:3]]
            public["content_status"] = "partial" if public["coverage"]["partial"] or public["counts"]["held"] else "normal" if selected else "no_news"
            new_hash = approval_digest(public, candidates, record["batch_ids"])
            ready = action == "approve"
            db.execute("UPDATE issues SET state=?,content_hash=?,approval_hash=?,payload=?,candidates=?,updated_at=? WHERE issue_id=? AND revision=?", ["READY" if ready else "REVIEW_REQUIRED", new_hash, new_hash if ready else None, canonical(public), canonical(candidates), utcnow(), issue_id, record["revision"]])
            db.execute("INSERT INTO editorial_actions VALUES (?,?,?,?,?,?,?,?,?)", [digest({"issue": issue_id, "old": record["content_hash"], "new": new_hash, "at": utcnow()}), issue_id, record["revision"], action, clean(request.get("actor", "authenticated_editor"))[:100], clean(request.get("reason", ""))[:1000], record["content_hash"], new_hash, utcnow()])
            return self.store._issue(db.execute("SELECT * FROM issues WHERE issue_id=? AND revision=?", [issue_id, record["revision"]]).fetchone())
        return self.store.call(work, write=True)

    @staticmethod
    def _replace_representative(db, candidate, article_id):
        row = db.execute("SELECT metadata,evidence,content_hash FROM articles WHERE article_id=?", [article_id]).fetchone()
        if not row:
            raise ValueError("article_not_found")
        article, evidence = json.loads(row[0]), json.loads(row[1])
        facts = classify({**article, **evidence})
        candidate["item"].update(representative_article_id=article_id, headline=article["title"], summary=clean(evidence.get("text") or evidence.get("description") or article["title"])[:280], source_name=article["source_name"], source_url=article["url"], source_published_at=article["source_published_at"], timestamp_basis=article["timestamp_basis"], evidence_scope=article["evidence_scope"], project_name=article.get("project_name"), companies=article.get("companies", []), region=article.get("region"), contract_stage=facts["contract_stage"], primary_category=facts["primary_category"], wind_type=facts["wind_type"])
        candidate.update(evidence_hash=row[2], approved=False, held=True, review_required=True)

    def purge_expired_evidence(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=min(30, self.policy.get("raw_text_retention_days", 30)))
        def work(db):
            expired = db.execute("SELECT article_id,evidence FROM articles WHERE updated_at<?", [cutoff.isoformat()]).fetchall()
            for article_id, raw in expired:
                evidence = json.loads(raw)
                evidence.pop("text", None)
                evidence.pop("description", None)
                db.execute("UPDATE articles SET evidence=? WHERE article_id=?", [canonical(evidence), article_id])
                snapshots = db.execute("SELECT batch_id,article_snapshot FROM batch_articles WHERE article_id=?", [article_id]).fetchall()
                for batch_id, snapshot in snapshots:
                    if snapshot:
                        content = json.loads(snapshot)
                        content.pop("text", None)
                        content.pop("description", None)
                        db.execute("UPDATE batch_articles SET article_snapshot=? WHERE batch_id=? AND article_id=?", [canonical(content), batch_id, article_id])
            db.execute("UPDATE jobs SET payload='{}',status=CASE WHEN status IN ('QUEUED','RUNNING') THEN 'FAILED' ELSE status END,error_code=CASE WHEN status IN ('QUEUED','RUNNING') THEN 'RAW_EVIDENCE_EXPIRED' ELSE error_code END WHERE operation='ingest' AND created_at<?", [cutoff.isoformat()])
            return len(expired)
        return self.store.call(work, write=True)
