"""Synthetic data for isolated tests only; never a national production dataset."""
from wind_map.core import date_value
from wind_map.snapshot import review_digest
NOW = "2026-11-12T12:00:00+09:00"
PERMIT = {"id": "siru-permit", "project_id": "siru", "authority": "전기위원회", "number": "2026-16"}

def fact(ident, kind, date="2026-05-13", **extra):
    return {"id": ident, "type": kind, "date": date_value(date), "known_at": NOW,
            "review_state": "approved", "evidence_id": "source-siru", "permit_id": PERMIT["id"], **extra}

def seed(store, *, approved=True):
    doc_id = store.register_document(b"synthetic register fixture, not an actual PDF", {
        "source_key": "test-register", "agency": "검증용 기관", "title": "검증용 허가대장 표본",
        "url": "https://www.korec.go.kr/notice/moveNoticeDetail.do?bbsSntncNo=509&bbsSe=0",
        "checked_at": NOW, "visibility": "public", "format": "pdf", "purpose": "permit_register",
        "quarter": "2026-Q3", "page_count": 1})
    regions = [{"id":"KR","name":"전국","parent_id":None,"code_year":2026},
               {"id":"47","name":"경상북도","parent_id":"KR","code_year":2026},
               {"id":"47130","name":"경주시","parent_id":"47","code_year":2026},
               {"id":"47770","name":"영덕군","parent_id":"47","code_year":2026},
               {"id":"42","name":"강원특별자치도","parent_id":"KR","code_year":2026}]
    bundle = {"schema_version":1,
        "projects":[{"id":"siru","name":"경주시루풍력 (검증용)","name_evidence_id":"source-siru","wind_type":"onshore","name_kind":"register_name","aliases":[]}],
        "regions":regions,
        "evidence":[{"id":"source-siru","document_id":doc_id,"page":1,"quote":"검증용 표본: 2026-16, 육상풍력 60 MW. 실제 전국 통계가 아닙니다.","publication_approved":True}],
        "permits":[PERMIT],
        "events":[fact("grant-siru","grant",capacity_w=60_000_000,is_initial=True,preparation_deadline=date_value("2029-12-31"),construction_plan_deadline=date_value("2027-12-31"))],
        "allocation_sets":[{"id":"allocation-siru","permit_id":PERMIT["id"],"date":date_value("2026-05-13"),"known_at":NOW,"review_state":"approved","evidence_id":"source-siru",
            "capacity_event_id":"grant-siru","parts":[{"region_id":"47130","capacity_w":60_000_000}]}],
        "coverage":[{"id":"coverage-"+r["id"],"region_id":r["id"],"status":"central_only" if r["id"] != "42" else "not_collected","as_of":"2026-09-30","scope":"over_3mw","quarter":"2026-Q3"} for r in regions]}
    store.import_bundle(bundle,actor="test",reason="synthetic fixture")
    store.record_page(doc_id,1,[{"classification":"candidate","text":"synthetic"}],status="extracted")
    if approved:
        store.approve_page(doc_id,1,actor="test",reason="synthetic fixture")
        store.import_bundle({"schema_version":1,"validations":[{"id":kind,"document_id":doc_id,"kind":kind,"approved":True,
            "source_sha256":store.document(doc_id)["sha256"],"actor":"test","checked_at":NOW,
            "review_digest":review_digest(store,doc_id),
            "row_links":[{"row_id":store.db.execute("SELECT id FROM extracted_rows WHERE document_id=?",(doc_id,)).fetchone()[0],
                          "permit_id":PERMIT["id"],"evidence_id":"source-siru"}],
            "checks":[{"table":"events","record_id":"grant-siru","field":"capacity_w","expected":60_000_000}]} for kind in ["golden_samples","row_reconciliation"]]},actor="test",reason="synthetic validation")
    return doc_id, bundle