"""Private tracking inventory including unresolved observations; never a publication snapshot."""
from collections import defaultdict
import hashlib
import html
from pathlib import Path

from .core import WindError, atomic_write, canonical, utc_now


def make_tracking(store):
    # Read all tables from one SQLite snapshot, without taking a writer lock.
    store.db.execute("BEGIN")
    try:
        projects = store.all("projects")
        permits = store.all("permits")
        events = store.all("events")
        milestones = store.all("milestones")
        evidence = {v["id"]: v for v in store.all("evidence")}
        documents = {v["id"]: v for v in store.all("documents")}
    finally:
        store.db.rollback()
    permits_by_project, events_by_permit, milestones_by_project = defaultdict(list), defaultdict(list), defaultdict(list)
    for item in permits:
        permits_by_project[item.get("project_id")].append(item)
    for item in events:
        events_by_permit[item.get("permit_id")].append(item)
    for item in milestones:
        milestones_by_project[item.get("project_id")].append(item)
    # Some source observations deliberately point *toward* a candidate project
    # or corroborate an existing event without changing that immutable event.
    # Surface those links as candidates; do not turn them into approved facts.
    candidate_sources = defaultdict(set)
    event_projects = {event.get("id"): permit.get("project_id")
                      for permit in permits for event in events_by_permit[permit.get("id")]}
    for ident, item in evidence.items():
        for key in ("project_id_candidate", "candidate_project_id"):
            if isinstance(item.get(key), str):
                candidate_sources[item[key]].add(ident)
        event_id = item.get("confirms_existing_event_id")
        if event_id in event_projects and event_projects[event_id]:
            candidate_sources[event_projects[event_id]].add(ident)
    records = []
    for project in projects:
        if project.get("wind_type") == "offshore":
            continue
        project_permits = permits_by_project[project["id"]]
        project_events = [e for p in project_permits for e in events_by_permit[p["id"]]]
        project_milestones = milestones_by_project[project["id"]]
        refs = set()
        for obj in [project, *project_permits, *project_events, *project_milestones]:
            for key, value in obj.items():
                if (key == "evidence_id" or key.endswith("_evidence_id")) and isinstance(value, str):
                    refs.add(value)
                elif key.endswith("_evidence_ids") and isinstance(value, list):
                    refs.update(x for x in value if isinstance(x, str))
        direct_refs = set(refs)
        refs.update(candidate_sources[project["id"]])
        sources = []
        for ident in sorted(refs):
            if ident not in evidence:
                continue
            item = evidence[ident]
            document = documents.get(item.get("document_id"), {})
            sources.append({"evidence": item, "candidate_link": ident not in direct_refs,
                            "document": {k: document.get(k) for k in
                ("id", "title", "agency", "url", "sha256", "checked_at")}})
        records.append({"project": project, "scope": "onshore" if project.get("wind_type") == "onshore" else "unknown",
                        "permits": project_permits, "events": project_events,
                        "milestones": project_milestones, "sources": sources})
    records.sort(key=lambda x: (x["scope"], x["project"].get("name", ""), x["project"]["id"]))
    return {"schema_version": 1, "created_at": utc_now(), "records": records,
            "counts": {"onshore_observations": sum(x["scope"] == "onshore" for x in records),
                       "scope_unconfirmed_observations": sum(x["scope"] == "unknown" for x in records)},
            "limitations": ["미확정 유형은 육상으로 확정하지 않음", "중복 가능 관측은 독립 사업 수나 용량으로 합산하지 않음",
                            "계획·공란·준공 보고로 허가 승인이나 사용전검사를 추정하지 않음"]}


def _render(data):
    esc = lambda value: html.escape(str(value if value is not None else "미확인"), quote=True)
    projects_by_id = {record["project"]["id"]: record["project"] for record in data["records"]}
    out = ['<!doctype html><html lang="ko"><meta charset="utf-8"><title>비공개 풍력 추적</title>',
           '<style>body{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#172238}'
           'input{padding:.6rem;width:min(95%,38rem)}article{border:1px solid #ccd5df;border-radius:8px;'
           'padding:1rem;margin:1rem 0}.tag{background:#edf2f8;padding:.2rem .5rem;border-radius:4px}'
           'small{color:#526174}li{margin:.4rem 0}</style><h1>비공개 풍력 추적</h1>',
           '<p>작성 시점 ', esc(data["created_at"]), ' · 육상 관측 ', str(data["counts"]["onshore_observations"]),
           ' · 유형 확인 필요 ', str(data["counts"]["scope_unconfirmed_observations"]), '</p>',
           '<p>유형 미확정 관측은 육상 확정이 아닙니다. 중복 가능성이 있어 건수·용량을 합산하지 않습니다. '
           '신청·승인·사용전검사는 근거를 따로 확인해야 합니다.</p>',
           '<label>사업명·지역 검색 <input id="q" type="search" placeholder="사업자, 사업명, 소재지"></label><p id="count"></p><main>']
    for record in data["records"]:
        p = record["project"]
        tags = ["육상" if record["scope"] == "onshore" else "유형 확인 필요"]
        if p.get("project_identity_unconfirmed") or p.get("permit_link_unconfirmed"):
            tags.append("허가 연결 미확정")
        if p.get("possible_duplicate_not_resolved") or p.get("not_independent_capacity_for_aggregate"):
            tags.append("중복 가능")
        out += ['<article id="project-', esc(p["id"]), '"><h2>', esc(p.get("name")), '</h2><p>',
                ' '.join('<span class="tag">' + esc(t) + '</span>' for t in tags),
                '</p><p>소재지 관측: ', esc(p.get("location_text")), '</p><small>', esc(p["id"]), '</small>']
        for key in ("candidate_scope_note", "candidate_external_status_scope", "reported_capacity_conflict",
                    "source_location_conflict", "reported_cross_source_dates_note", "wind_type_reason"):
            if p.get(key):
                out += ['<p>', esc(p[key]), '</p>']
        for link in p.get("same_observation_links", []):
            target = projects_by_id.get(link.get("project_id"))
            if target:
                out += ['<p>같은 대장행이 연결된 기존 사업: <a href="#project-',
                        esc(target["id"]), '">', esc(target.get("name")), '</a>',
                        ' · 허가증 전체의 동일성은 별도 확인</p>']
        for link in p.get("candidate_project_matches", []):
            target = projects_by_id.get(link.get("project_id"))
            if target:
                out += ['<p>관련 사업 후보: <a href="#project-', esc(target["id"]), '">',
                        esc(target.get("name")), '</a> · ', esc(link.get("basis_note", "")),
                        ' · 동일 허가 여부 미확정</p>']
        out += ['<details><summary>대장 이력 ', str(len(record["events"])), '건 · 마일스톤 ',
                str(len(record["milestones"])), '건 · 근거 ', str(len(record["sources"])), '건</summary>']
        for permit in record["permits"]:
            out += ['<p>허가 번호 관측: ', esc(permit.get("number")), ' · ', esc(permit.get("authority")),
                    ' · 번호 근거 유형 ', esc(permit.get("identifier_kind") or "미표기"), '</p>']
        for item in [*record["events"], *record["milestones"]]:
            date = item.get("date") or {}
            out += ['<p>', esc(date.get("start") or "일자 미확인"), ' · ', esc(item.get("type")),
                    ' · 검토 ', esc(item.get("review_state")), ' · ', esc(item.get("raw_disposition") or "")]
            if type(item.get("capacity_w")) is int:
                out += [' · 용량 관측 ', esc(item["capacity_w"] / 1_000_000), ' MW']
            for field, label in (("preparation_deadline", "사업준비기한"),
                                 ("construction_plan_deadline", "공사계획 인가기한")):
                deadline = item.get(field) or {}
                if deadline.get("end"):
                    out += [' · ', label, ' ', esc(deadline["end"])]
            out.append('</p>')
        for source in record["sources"]:
            e, doc = source["evidence"], source["document"]
            out += ['<p><strong>', esc(doc.get("title") or "출처"), '</strong> · ',
                    '공개 승인 근거' if e.get("publication_approved") is True else '검증 대기 근거',
                    ' · 연결 후보' if source.get("candidate_link") else '',
                    (' · ' + esc(e["page"]) + '쪽') if e.get("page") else '', '<br>', esc(e.get("quote") or "")]
            limits = e.get("limits") or e.get("use_limit")
            if limits:
                if isinstance(limits, list):
                    limits = "; ".join(str(item) for item in limits)
                out += ['<br><small>확인 범위: ', esc(limits), '</small>']
            out.append('</p>')
        out.append('</details></article>')
    out.append('</main><script>const q=document.querySelector("#q"),a=[...document.querySelectorAll("article")],'
               'n=document.querySelector("#count");function f(){let v=q.value.trim().toLowerCase(),k=0;'
               'for(const x of a){x.hidden=!x.textContent.toLowerCase().includes(v);if(!x.hidden)k++}'
               'n.textContent=`표시 ${k}건`};q.addEventListener("input",f);f();</script></html>')
    return ''.join(out).encode('utf-8')


def export_tracking(store, output):
    operator = (store.root / "operator").resolve()
    target = Path(output).resolve()
    if not operator.is_relative_to(store.root) or target == operator or not target.is_relative_to(operator):
        raise WindError("Tracking export must be in a new directory inside private runtime/operator")
    if target.exists():
        raise WindError("Tracking export destination already exists")
    data = make_tracking(store)
    payload, page = canonical(data), _render(data)
    target.mkdir(parents=True, exist_ok=False)
    atomic_write(target / "tracking.json", payload)
    atomic_write(target / "tracking.html", page)
    manifest = {"schema_version": 1, "created_at": data["created_at"], "counts": data["counts"],
                "sha256": {"tracking.json": hashlib.sha256(payload).hexdigest(),
                           "tracking.html": hashlib.sha256(page).hexdigest()}}
    atomic_write(target / "manifest.json", canonical(manifest))
    return {"output": str(target), "counts": data["counts"]}
