from contextlib import closing
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from wind_map.core import WindError, capacity_w, date_value, confirmed_by, safe_url, canonical
from wind_map.events import permit_state, selected_facts, validate_lineage
from wind_map.aggregates import allocations_for, build_totals, region_tree
from wind_map.extract import parse_tables, register_date, hwpx_text, compare_registers, import_page_review
from wind_map.geography import wgs84, coordinate_for_display
from wind_map.matching import match_project
from wind_map.milestones import validate_milestone, infer_start_window, inspection_summary
from wind_map.store import Store
from wind_map.snapshot import build_snapshot, write_snapshot, publication_gates
from wind_map.publication import validate_tree, rollback, validate_descriptor
from wind_map.queue import SearchQueue
from wind_map.sources import parse_open_portal, parse_html_page, korec_attachments, run_job
from wind_map_helpers import seed, fact, PERMIT, NOW

FIXTURES = Path(__file__).parent / "fixtures" / "wind_map"


class ExactValueTests(unittest.TestCase):
    def test_units_and_decimal_capacity(self):
        for text, expected in [("98,000kW",98_000_000),("4만 kW",40_000_000),("60MW",60_000_000),(".000001MW",1)]:
            self.assertEqual(capacity_w(text),expected)
    def test_reject_inexact_ambiguous_or_overflow_capacity(self):
        for value in [1.1, True, "-60MW", "60", "0.0000001MW", "9007199254740992W", "6O MW"]:
            with self.subTest(value=value), self.assertRaises(WindError): capacity_w(value)
    def test_month_precision_and_unknown(self):
        self.assertEqual(date_value("2024-02")["end"],"2024-02-29")
        self.assertEqual(date_value()["precision"],"unknown")
        self.assertFalse(confirmed_by(date_value("2026-05"),"2026-05-15"))
        self.assertFalse(confirmed_by(date_value("2026-05-01",precision="after"),"2026-06-01"))
    def test_invalid_dates(self):
        for value in ["2026-02-30","2026-13","20260513"]:
            with self.subTest(value=value), self.assertRaises(WindError): date_value(value)
    def test_url_boundaries(self):
        self.assertEqual(safe_url("https://www.korec.go.kr/a"),"https://www.korec.go.kr/a")
        for value in ["http://example.com","https://127.0.0.1/a","https://localhost/a","https://a:b@example.com","https://example.com/?token=x","https://example.com:bad/a"]:
            with self.subTest(value=value), self.assertRaises(WindError): safe_url(value)


class PermitReplayTests(unittest.TestCase):
    def state(self, events, date="2026-11-12", known=NOW):
        return permit_state(PERMIT,events,date,known)
    def test_capacity_extension_and_rename(self):
        events=[fact("g","grant",capacity_w=50_000_000,is_initial=True),fact("c","capacity_change","2026-06-01",capacity_w=60_000_000,previous_capacity_w=50_000_000),
                fact("x","extend","2026-07-01",preparation_deadline=date_value("2029-12-31")),fact("n","rename","2026-08-01",name="새 이름")]
        s=self.state(events)
        self.assertEqual(s["capacity_w"],60_000_000)
        self.assertEqual(s["first_ebl_date"]["start"],"2026-05-13")
        self.assertEqual(s["capacity_ebl_date"]["start"],"2026-06-01")
        self.assertEqual(s["name"],"새 이름")
    def test_backfilled_old_register_cannot_resurrect_cancelled_permit(self):
        events=[fact("cancel","cancel","2026-07-01"),fact("old","registry_observation","2026-06-30",capacity_w=60_000_000)]
        self.assertEqual(self.state(events,"2026-06-30")["capacity_w"],60_000_000)
        self.assertEqual(self.state(events)["capacity_w"],0)
        self.assertEqual(self.state(events)["status"],"cancelled")
    def test_registry_after_cancellation_is_conflict_not_resurrection(self):
        s=self.state([fact("cancel","cancel","2026-07-01"),fact("old","registry_observation","2026-09-30",capacity_w=60_000_000)])
        self.assertEqual(s["capacity_w"],0)
        self.assertTrue(s["conflicts"])
    def test_expiry_is_review_not_cancellation(self):
        s=self.state([fact("g","grant",capacity_w=60_000_000,is_initial=True,preparation_deadline=date_value("2026-06-01"))])
        self.assertEqual((s["status"],s["capacity_w"]),("review",60_000_000))
    def test_business_commencement_resolves_expiry(self):
        s=self.state([fact("g","grant",capacity_w=60_000_000,is_initial=True,preparation_deadline=date_value("2026-06-01")),
                      fact("start","business_commenced","2026-05-30")])
        self.assertEqual(s["status"],"valid")
    def test_unapproved_and_future_knowledge_excluded(self):
        s=self.state([fact("g","grant",capacity_w=60_000_000,is_initial=True,review_state="pending")])
        self.assertFalse(s["capacity_known"])
        self.assertFalse(self.state([fact("g","grant",capacity_w=60_000_000)],known="2026-01-01T00:00:00Z")["capacity_known"])
    def test_approved_pending_not_valid_until_grant(self):
        events=[fact("p","approved_pending","2026-04-01",capacity_w=80_000_000),fact("g","grant","2026-06-01",capacity_w=80_000_000,is_initial=True)]
        self.assertEqual(self.state(events,"2026-05-01")["status"],"approved_pending")
        self.assertEqual(self.state(events)["status"],"valid")
    def test_register_report_date_is_not_first_ebl(self):
        s=self.state([fact("r","registry_observation",capacity_w=60_000_000)])
        self.assertIsNone(s["first_ebl_date"])
        self.assertIsNone(s["capacity_ebl_date"])
    def test_correction_is_known_time_sensitive(self):
        g=fact("g","grant",capacity_w=50_000_000)
        correction=fact("correct","grant",capacity_w=60_000_000,supersedes="g",known_at="2026-12-01T00:00:00Z")
        self.assertEqual(self.state([g,correction])["capacity_w"],50_000_000)
        self.assertEqual(self.state([g,correction],known="2026-12-02T00:00:00Z")["capacity_w"],60_000_000)
    def test_same_date_conflict_and_bad_baseline(self):
        s=self.state([fact("a","grant",capacity_w=50_000_000),fact("b","grant",capacity_w=60_000_000)])
        self.assertEqual(s["status"],"review")
        self.assertTrue(s["conflicts"])
        s=self.state([fact("a","grant",capacity_w=50_000_000),fact("c","capacity_change","2026-06-01",capacity_w=60_000_000,previous_capacity_w=40_000_000)])
        self.assertIn("capacity_change_baseline_mismatch",s["conflicts"])
    def test_unknown_date_does_not_invent_an_effective_day(self):
        s=self.state([fact("g","grant",None,capacity_w=60_000_000)])
        self.assertTrue(s["capacity_known"])
        self.assertEqual(s["capacity_w"],60_000_000)
        self.assertEqual(s["status"],"review")
        self.assertIsNone(s["first_ebl_date"])
    def test_separate_plan_deadline(self):
        s=self.state([fact("g","grant",capacity_w=60_000_000,construction_plan_deadline=date_value("2027-12-31"),preparation_deadline=date_value("2029-12-31"))])
        self.assertEqual(s["preparation_deadline"]["end"],"2029-12-31")
        self.assertEqual(s["construction_plan_deadline"]["end"],"2027-12-31")
    def test_lineage_conserves_capacity(self):
        close=fact("close","split_close","2026-08-01",lineage_id="split",successors=["a","b"],previous_capacity_w=60_000_000)
        a=fact("ga","grant","2026-08-01",permit_id="a",lineage_id="split",capacity_w=35_000_000)
        b=fact("gb","grant","2026-08-01",permit_id="b",lineage_id="split",capacity_w=25_000_000)
        states=[{"permit_id":p} for p in [PERMIT["id"],"a","b"]]
        validate_lineage(states,[fact("initial","grant",capacity_w=60_000_000),close,a,b],"2026-11-12",NOW)
        b["capacity_w"]=26_000_000
        with self.assertRaises(WindError): validate_lineage(states,[fact("initial","grant",capacity_w=60_000_000),close,a,b],"2026-11-12",NOW)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/"runtime")
        self.doc,self.bundle=seed(self.store)
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def test_identical_import_is_idempotent(self):
        before=build_snapshot(self.store,"2026-11-12",NOW)
        self.assertEqual(self.store.import_bundle(self.bundle,actor="test",reason="repeat"),0)
        after=build_snapshot(self.store,"2026-11-12",NOW)
        self.assertEqual(before["snapshot_id"],after["snapshot_id"])
    def test_fact_mutation_rejected(self):
        event=dict(self.bundle["events"][0],capacity_w=600_000_000)
        with self.assertRaises(WindError): self.store.import_bundle({"schema_version":1,"events":[event]},actor="test",reason="bad update")
    def test_import_rolls_back_all_tables_on_failure(self):
        bundle={"schema_version":1,"projects":[{"id":"new","name":"test"}],"events":[dict(self.bundle["events"][0],capacity_w=1)]}
        with self.assertRaises(WindError): self.store.import_bundle(bundle,actor="test",reason="rollback")
        self.assertNotIn("new",{p["id"] for p in self.store.all("projects")})
    def test_sqlite_backup_is_consistent(self):
        path=Path(self.tmp.name)/"backup.sqlite3"
        self.store.backup(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM permits").fetchone()[0],1)
    def test_current_snapshot_and_private_data_exclusion(self):
        snap=build_snapshot(self.store,"2026-11-12",NOW)
        raw=b"".join(snap["files"].values()).decode()
        self.assertNotIn("originals/",raw);self.assertNotIn("wind-map.sqlite3",raw)
        detail=json.loads(snap["files"]["projects/siru.json"])
        self.assertEqual(detail["permits"][0]["capacity_w"],60_000_000)
        self.assertIsNone(detail["coordinate"])
        self.assertEqual(detail["milestones"],[])
    def test_restricted_evidence_cannot_be_published(self):
        doc=self.store.document(self.doc);doc["visibility"]="restricted"
        self.store.db.execute("UPDATE documents SET data=? WHERE id=?",(canonical(doc).decode(),self.doc))
        with self.assertRaises(WindError):build_snapshot(self.store,"2026-11-12",NOW)
    def test_missing_page_blocks_publication(self):
        self.store.db.execute("DELETE FROM extracted_rows")
        self.store.db.execute("DELETE FROM pages")
        with self.assertRaises(WindError):build_snapshot(self.store,"2026-11-12",NOW)
        self.assertTrue(build_snapshot(self.store,"2026-11-12",NOW,preview=True)["manifest"]["preview"])
    def test_unreviewed_ocr_page_blocks_publication(self):
        self.store.db.execute("UPDATE pages SET status='needs_ocr',reviewed=0")
        self.assertTrue(publication_gates(self.store))
    def test_preview_cannot_be_written_as_production(self):
        snap=build_snapshot(self.store,"2026-11-12",NOW,preview=True)
        with self.assertRaises(WindError):write_snapshot(snap,Path(self.tmp.name)/"public")
    def test_immutable_publication_and_corruption_detection(self):
        root=Path(self.tmp.name)/"public"
        snap=build_snapshot(self.store,"2026-11-12",NOW)
        pointer=write_snapshot(snap,root);validate_tree(root)
        self.assertEqual(write_snapshot(snap,root),pointer)
        path=root/"data/wind-map/versions"/snap["snapshot_id"]/"index.json"
        path.write_bytes(b"corrupt")
        with self.assertRaises(WindError):validate_tree(root)
        with self.assertRaises(WindError):write_snapshot(snap,root)
    def test_rollback_to_previous_verified_version(self):
        root=Path(self.tmp.name)/"public"
        first=build_snapshot(self.store,"2026-11-11",NOW);second=build_snapshot(self.store,"2026-11-12",NOW)
        write_snapshot(first,root);write_snapshot(second,root)
        self.assertEqual(rollback(root)["current"]["snapshot_id"],first["snapshot_id"])
    def test_invalid_new_snapshot_never_changes_pointer(self):
        root=Path(self.tmp.name)/"public"
        first=build_snapshot(self.store,"2026-11-11",NOW);write_snapshot(first,root)
        path=root/"data/wind-map/current.json";before=path.read_bytes()
        bad=build_snapshot(self.store,"2026-11-12",NOW)
        bad["files"]["index.json"]=b'{"schema_version":1}'
        with self.assertRaises(WindError):write_snapshot(bad,root)
        self.assertEqual(path.read_bytes(),before)
    def test_region_split_deduplicates_parent_permits(self):
        state=permit_state(PERMIT,self.bundle["events"],"2026-11-12",NOW)
        alloc=copy.deepcopy(self.bundle["allocation_sets"][0])
        alloc["parts"]=[{"region_id":"47130","capacity_w":35_000_000},{"region_id":"47770","capacity_w":25_000_000}]
        regions=region_tree(self.bundle["regions"])
        state["allocations"]=allocations_for(state,[alloc],regions,"2026-11-12",NOW)
        totals={r["region_id"]:r for r in build_totals([state],self.bundle["regions"],[])["valid"]}
        self.assertEqual(totals["47"]["capacity_w"],60_000_000)
        self.assertEqual(totals["47"]["permit_count"],1)
        self.assertEqual(totals["47130"]["capacity_w"],35_000_000)
        self.assertIsNone(totals["42"]["display_capacity_w"])
    def test_lowest_known_region_unallocated(self):
        state=permit_state(PERMIT,self.bundle["events"],"2026-11-12",NOW)
        alloc=copy.deepcopy(self.bundle["allocation_sets"][0]);alloc["parts"]=[{"region_id":"47","capacity_w":60_000_000}]
        state["allocations"]=allocations_for(state,[alloc],region_tree(self.bundle["regions"]),"2026-11-12",NOW)
        totals={r["region_id"]:r for r in build_totals([state],self.bundle["regions"],[])["valid"]}
        self.assertEqual(totals["47"]["unallocated_w"],60_000_000)
        self.assertEqual(totals["KR"]["capacity_w"],60_000_000)
    def test_invalid_allocation_sum_blocks(self):
        state=permit_state(PERMIT,self.bundle["events"],"2026-11-12",NOW)
        alloc=copy.deepcopy(self.bundle["allocation_sets"][0]);alloc["parts"][0]["capacity_w"]=59_000_000
        with self.assertRaises(WindError):allocations_for(state,[alloc],region_tree(self.bundle["regions"]),"2026-11-12",NOW)
    def test_coordinate_error_preserves_permit_capacity(self):
        c={"id":"bad-point","project_id":"siru","date":date_value("2026-05-13"),"known_at":NOW,"review_state":"approved","evidence_id":"source-siru","kind":"parcel","x":35,"y":129,"region_check":"inside"}
        self.store.import_bundle({"schema_version":1,"coordinates":[c]},actor="test",reason="bad axes")
        detail=json.loads(build_snapshot(self.store,"2026-11-12",NOW)["files"]["projects/siru.json"])
        self.assertIsNone(detail["coordinate"]);self.assertTrue(detail["coordinate_warning"])
        self.assertEqual(detail["permits"][0]["capacity_w"],60_000_000)

class ActualRegisterTests(unittest.TestCase):
    def rows(self, number):
        data=json.loads((FIXTURES/f"page-{number}.json").read_text(encoding="utf-8"))
        return parse_tables(data["tables"],data["text"])[0]
    def test_siru_actual_row(self):
        rows=self.rows(805)
        siru=next(r for r in rows if r.get("candidate",{}).get("name")=="경주시루풍력")
        c=siru["candidate"]
        self.assertEqual(siru["classification"],"candidate")
        self.assertEqual((c["permit_year"],c["number"],c["capacity_w"]),("2026","16",60_000_000))
        self.assertEqual(c["permit_or_change_date"]["start"],"2026-05-13")
        self.assertEqual(c["preparation_deadline"]["end"],"2029-12-31")
        self.assertEqual(c["construction_plan_deadline"]["end"],"2027-12-31")
        self.assertNotIn("125°",c["location"])
        self.assertEqual(rows[1]["issue"],"detached_continuation_or_ditto")
    def test_old_kw_and_ten_thousand_units(self):
        wind=next(r["candidate"] for r in self.rows(3) if r.get("candidate",{}).get("name")=="강원풍력발전(주)")
        self.assertEqual(wind["capacity_w"],98_000_000)
        self.assertEqual(wind["permit_year"],"2002")
        yd=next(r["candidate"] for r in self.rows(4) if r.get("candidate",{}).get("name")=="영덕풍력발전(주)")
        self.assertEqual(yd["capacity_w"],40_000_000)
    def test_old_month_precision(self):
        c=next(r["candidate"] for r in self.rows(4) if r.get("candidate",{}).get("number")=="1")
        self.assertEqual(c["permit_or_change_date"]["precision"],"month")
        self.assertEqual(c["permit_or_change_date"]["start"],"2003-02-01")
    def test_repeated_numbers_do_not_collapse_rows(self):
        rows=[r for r in self.rows(3) if r.get("candidate",{}).get("number")=="8"]
        self.assertEqual(len(rows),3)
        self.assertEqual(len({r["candidate"]["name"] for r in rows}),3)
    def test_all_rows_accounted_for(self):
        for number in [1,3,4,805,860]:
            data=json.loads((FIXTURES/f"page-{number}.json").read_text(encoding="utf-8"))
            rows,_=parse_tables(data["tables"],data["text"])
            self.assertEqual(len(rows),sum(len(t) for t in data["tables"]))
            self.assertTrue(all(r["classification"] in {"candidate","out_of_scope","header","blank","review"} for r in rows))
    def test_page_860_has_only_blank_and_header_but_requires_image_review(self):
        rows=self.rows(860)
        self.assertTrue(all(r["classification"] in {"blank","header"} for r in rows))
        provenance=json.loads((FIXTURES/"provenance.json").read_text(encoding="utf-8"))
        self.assertIn(860,provenance["visually_checked"])
    def test_missing_next_quarter_is_followup(self):
        rows=self.rows(805)
        changes=compare_registers(rows,[])
        self.assertTrue(all(c["kind"]=="missing_requires_followup" for c in changes))
    def test_hwpx_safe_text_and_entity_rejection(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,"w") as z:z.writestr("Contents/section0.xml",'<p xmlns:hp="x"><hp:t>허가 확인</hp:t></p>')
        self.assertEqual(hwpx_text(stream.getvalue()),"허가 확인")
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,"w") as z:z.writestr("Contents/section0.xml",'<!DOCTYPE x [<!ENTITY z SYSTEM "file:///secret">]><x/>')
        with self.assertRaises(WindError):hwpx_text(stream.getvalue())


class MilestoneAndLocationTests(unittest.TestCase):
    def milestone(self, **extra):
        return {"id":"m","type":"environment","status":"completed","date":date_value("2026-09-01"),
                "permit_ids":["p"],"evidence_id":"e","assessment_type":"main","stage":"main",**extra}
    def test_environment_type_and_scope_mandatory(self):
        validate_milestone(self.milestone(),{"p"})
        for changes in [{"assessment_type":"unknown"},{"stage":"unknown"},{"permit_ids":["other"]},{"status":"not_applied"}]:
            with self.subTest(changes=changes), self.assertRaises(WindError):validate_milestone(self.milestone(**changes),{"p"})
    def test_explicit_non_application_not_search_zero(self):
        validate_milestone(self.milestone(status="not_applied",explicit_negative=True),{"p"})
    def test_construction_month_needs_two_bounds(self):
        a={"status":"not_started","date":date_value("2026-06-03"),"evidence_id":"a"}
        b={"status":"in_progress","date":date_value("2026-06-25"),"evidence_id":"b"}
        self.assertEqual(infer_start_window(a,b)["inferred_month"],"2026-06")
        b["date"]=date_value("2026-07-01")
        self.assertIsNone(infer_start_window(a,b)["inferred_month"])
    def test_inspection_application_is_not_completion(self):
        row=self.milestone(type="inspection",status="applied",round=1,equipment=["u1"])
        summary=inspection_summary([row],[{"id":"u1","capacity_w":5_000_000,"evidence_id":"e"}],"2026-11-12")
        self.assertEqual(summary["passed_equipment"],[])
        self.assertFalse(summary["all_passed"])
    def test_inspected_equipment_deduplicated_across_rounds(self):
        a=self.milestone(type="inspection",status="passed",round=1,equipment=["u1"])
        b=self.milestone(id="m2",type="inspection",status="passed",round=2,equipment=["u1","u2"],date=date_value("2026-10-01"))
        units=[{"id":u,"capacity_w":5_000_000,"evidence_id":"e"} for u in ["u1","u2"]]
        result=inspection_summary([a,b],units,"2026-11-12")
        self.assertEqual(result["capacity_w"],10_000_000);self.assertTrue(result["all_passed"])
    def test_inspection_capacity_unknown_without_equipment_ratings(self):
        row=self.milestone(type="inspection",status="passed",round=1,equipment=["u1"])
        self.assertIsNone(inspection_summary([row],[],"2026-11-12")["capacity_w"])
    def test_same_name_or_same_operator_is_not_enough(self):
        p={"id":"a","name":"풍력","location":"경주","operator":"SPC","capacity_w":60}
        self.assertEqual(match_project({"name":"풍력"},[p])["state"],"review")
        self.assertEqual(match_project({"operator":"SPC","capacity_w":60,"location":"영덕"},[p])["state"],"review")
        self.assertEqual(match_project({"operator":"SPC","capacity_w":60},[p])["state"],"candidate")
        self.assertEqual(match_project({"operator":"SPC","capacity_w":60},[p,dict(p,id="b")])["state"],"review")
    def test_axes_head_office_and_region_review(self):
        self.assertEqual(wgs84(129,35)["longitude"],129)
        with self.assertRaises(WindError):wgs84(35,129)
        with self.assertRaises(WindError):coordinate_for_display({"kind":"parcel","address_role":"head_office"})
        with self.assertRaises(WindError):coordinate_for_display({"kind":"parcel","x":129,"y":35})


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.queue=SearchQueue(self.store)
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def page(self, ids, total, last=False):
        return {"items":[{"id":i} for i in ids],"total":total,"last_page":last,"complete_proof":"source_total" if last else None}
    def job(self):
        self.queue.enqueue("source","풍력","2025-01-01","2026-11-12")
        return self.queue.claim()
    def test_yearly_backfill_and_idempotency(self):
        ids=self.queue.backfill("source","풍력","2024-06-01","2026-11-12")
        self.assertEqual(len(ids),3)
        self.assertEqual(ids,self.queue.backfill("source","풍력","2024-06-01","2026-11-12"))
        self.assertEqual(len(self.queue.report()),3)
    def test_page_resume_and_proven_completion(self):
        job=self.job()
        self.assertFalse(self.queue.checkpoint(job,self.page(["1","2"],3)))
        self.assertEqual(job["cursor"],2)
        self.assertTrue(self.queue.checkpoint(job,self.page(["3"],3,True)))
        self.assertEqual(self.queue.report()[0]["status"],"done")
    def test_repeat_or_overlapping_page_never_success(self):
        job=self.job();self.queue.checkpoint(job,self.page(["1","2"],4))
        with self.assertRaises(WindError):self.queue.checkpoint(job,self.page(["1","2"],4,True))
        with self.assertRaises(WindError):self.queue.checkpoint(job,self.page(["2","3"],4,True))
    def test_empty_inaccessible_and_truncated_pages_rejected(self):
        job=self.job()
        for page in [{"items":[],"last_page":True},self.page([],5,True),{"items":[],"last_page":False}]:
            with self.subTest(page=page), self.assertRaises(WindError):self.queue.checkpoint(job,page)
    def test_verified_empty_result_is_success(self):
        job=self.job();self.assertTrue(self.queue.checkpoint(job,self.page([],0,True)))
    def test_lease_reclaim_prevents_stale_writer(self):
        self.queue.enqueue("source","풍력","2026-01-01","2026-11-12")
        old=self.queue.claim(now="2026-11-12T00:00:00+00:00",lease_seconds=1)
        new=self.queue.claim(now="2026-11-12T00:00:02+00:00")
        self.assertEqual(old["id"],new["id"])
        with self.assertRaises(WindError):self.queue.checkpoint(old,self.page([],0,True))
    def test_retry_and_split_preserve_checkpoint(self):
        job=self.job();self.queue.checkpoint(job,self.page(["1"],3));self.queue.fail(job,"RepeatedPage")
        self.queue.retry(job["id"])
        job=self.queue.claim();self.assertEqual(job["cursor"],2)
        self.queue.fail(job,"RepeatedPage")
        children=self.queue.split(job["id"]);self.assertEqual(len(children),2)
        self.assertEqual(next(r for r in self.queue.report() if r["id"] == job["id"])["status"],"split")
    def test_worker_failure_remains_blocked(self):
        self.job()
        self.store.db.execute("UPDATE queue SET status='pending'")
        result=run_job(self.store,{"source":{"hosts":["example.com"],"agency":"test"}},fetcher=lambda *_: (_ for _ in ()).throw(RuntimeError("private-token")))
        self.assertEqual(result["status"],"blocked")
        self.assertNotIn("private-token",str(self.queue.report()))


class SourceParserTests(unittest.TestCase):
    def test_portal_requires_list_and_total(self):
        with self.assertRaises(WindError):parse_open_portal({"error":"login"},query="풍력",cursor=1,page_size=100)
        with self.assertRaises(WindError):parse_open_portal({"rtnList":[]},query="풍력",cursor=1,page_size=100)
    def test_portal_privacy_and_stable_ids(self):
        result=parse_open_portal({"rtnList":[{"PRDCTN_INSTT_REGIST_NO":"id","INFO_SJ":"허가 요청","CHARGER_NM":"PRIVATE"}],"rtnTotal":1},query="풍력",cursor=1,page_size=100)
        self.assertEqual(result["items"][0]["status"],"candidate")
        self.assertNotIn("PRIVATE",str(result))
    def test_eiass_preserves_multiple_stages(self):
        html='<b class="total">2</b><table><tbody><tr><td>EA12345678</td><td>초안</td><td>풍력</td><td>2026-01-01</td></tr><tr><td>EA12345678</td><td>본안</td><td>풍력</td><td>2026-02-01</td></tr></tbody></table>'
        result=parse_html_page(html,{"kind":"eiass","assessment_type":"main","total_selector":".total","url":"https://www.eiass.go.kr/a","page_size":100},{"cursor":1})
        self.assertEqual(len(result["items"]),2)
        self.assertNotEqual(result["items"][0]["id"],result["items"][1]["id"])
    def test_korec_unicode_attachment_url(self):
        rows=korec_attachments('<input name="filePath" value="/notice"><input name="fileName" value="★대장.pdf">',"https://www.korec.go.kr/a")
        self.assertEqual(len(rows),1)
        self.assertIn("attachingFileNm=",rows[0]["url"])
        self.assertNotIn("★",rows[0]["url"])


class ReleaseBoundaryTests(unittest.TestCase):
    setUp = DatabaseTests.setUp
    tearDown = DatabaseTests.tearDown
    def test_backup_bundle_round_trip_and_no_overwrite(self):
        from wind_map.backup import backup_bundle, restore_bundle, verify_bundle
        root=Path(self.tmp.name)
        bundle=root/"bundle";restored=root/"restored"
        backup_bundle(self.store,bundle);verify_bundle(bundle);restore_bundle(bundle,restored)
        with Store(restored) as copy_store:
            self.assertEqual(build_snapshot(copy_store,"2026-11-12",NOW)["snapshot_id"],build_snapshot(self.store,"2026-11-12",NOW)["snapshot_id"])
        with self.assertRaises(WindError):restore_bundle(bundle,restored)
    def test_backup_tamper_blocks_restore(self):
        from wind_map.backup import backup_bundle, restore_bundle
        root=Path(self.tmp.name);bundle=root/"bundle"
        backup_bundle(self.store,bundle)
        doc=self.store.document(self.doc)
        (bundle/doc["asset"]).write_bytes(b"tampered")
        with self.assertRaises(WindError):restore_bundle(bundle,root/"restored")
        self.assertFalse((root/"restored").exists())
    def test_row_changes_invalidate_signed_reconciliation(self):
        self.store.db.execute("UPDATE extracted_rows SET data=?",(canonical({"classification":"candidate","text":"changed OCR"}).decode(),))
        with self.assertRaises(WindError):build_snapshot(self.store,"2026-11-12",NOW)
    def test_golden_value_mismatch_blocks(self):
        validation=next(v for v in self.store.all("validations") if v["kind"]=="golden_samples")
        validation["checks"][0]["expected"]=600_000_000
        self.store.import_bundle({"schema_version":1,"validations":[validation]},actor="test",reason="wrong OCR decimal")
        with self.assertRaises(WindError):build_snapshot(self.store,"2026-11-12",NOW)
    def test_private_extra_region_fields_are_not_exported(self):
        region=next(r for r in self.store.all("regions") if r["id"]=="47")
        region["internal_note"]="PRIVATE"
        self.store.import_bundle({"schema_version":1,"regions":[region]},actor="test",reason="private note")
        self.assertNotIn(b"PRIVATE",b"".join(build_snapshot(self.store,"2026-11-12",NOW)["files"].values()))
    def test_extra_date_fields_are_rejected(self):
        from wind_map.core import validate_date
        for value in [dict(date_value(),internal_note="PRIVATE"),dict(date_value("2026-01-02"),precision="month")]:
            with self.assertRaises(WindError):validate_date(value)
    def test_rollback_can_recover_corrupted_current(self):
        root=Path(self.tmp.name)/"public"
        first=build_snapshot(self.store,"2026-11-11",NOW);second=build_snapshot(self.store,"2026-11-12",NOW)
        write_snapshot(first,root);write_snapshot(second,root)
        (root/"data/wind-map/versions"/second["snapshot_id"]/"index.json").write_bytes(b"bad")
        self.assertEqual(rollback(root)["current"]["snapshot_id"],first["snapshot_id"])
        validate_tree(root)


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        import subprocess
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.remote=self.root/"remote.git";self.clone=self.root/"publisher"
        self.command("git","init","--bare","--initial-branch=main",str(self.remote))
        self.command("git","clone",str(self.remote),str(self.clone))
        self.config(self.clone)
        (self.clone/"README.md").write_text("Synthetic repository",encoding="utf-8")
        self.command("git","-C",str(self.clone),"add","README.md")
        self.command("git","-C",str(self.clone),"commit","-m","Initial fixture")
        self.command("git","-C",str(self.clone),"push","origin","main")
        (self.clone/".git/bre-wind-map-publisher").write_text(str(self.remote),encoding="utf-8")
        with Store(self.root/"runtime") as store:
            seed(store);self.snapshot=build_snapshot(store,"2026-11-12",NOW)
    def tearDown(self):
        self.tmp.cleanup()
    def command(self,*args):
        import subprocess
        result=subprocess.run(args,capture_output=True,text=True,timeout=30)
        if result.returncode:raise AssertionError(result.stderr)
        return result.stdout
    def config(self,path):
        self.command("git","-C",str(path),"config","user.name","Wind Map Test")
        self.command("git","-C",str(path),"config","user.email","test@example.invalid")
    def test_push_only_public_snapshot_and_idempotent_repeat(self):
        from wind_map.publication import publish
        result=publish(self.snapshot,self.clone,expected_remote=str(self.remote))
        self.assertEqual(result["status"],"pushed")
        paths=self.command("git","-C",str(self.clone),"diff-tree","--no-commit-id","--name-only","-r","HEAD").splitlines()
        self.assertTrue(paths);self.assertTrue(all(p.startswith("data/wind-map/") for p in paths))
        self.assertEqual(publish(self.snapshot,self.clone,expected_remote=str(self.remote))["status"],"unchanged")
    def test_dirty_clone_rejected(self):
        from wind_map.publication import publish
        (self.clone/"user.txt").write_text("user work",encoding="utf-8")
        with self.assertRaises(WindError):publish(self.snapshot,self.clone,expected_remote=str(self.remote))
        self.assertEqual((self.clone/"user.txt").read_text(),"user work")
    def test_competing_writer_rebases_without_force(self):
        from wind_map import publication
        other=self.root/"other"
        self.command("git","clone",str(self.remote),str(other));self.config(other)
        original=publication.git;attempts=[]
        def racing(root,*args,**kwargs):
            if args[0]=="push":
                attempts.append(args)
                if len(attempts)==1:
                    (other/"status.txt").write_text("other writer",encoding="utf-8")
                    self.command("git","-C",str(other),"add","status.txt")
                    self.command("git","-C",str(other),"commit","-m","Concurrent fixture")
                    self.command("git","-C",str(other),"push","origin","main")
            return original(root,*args,**kwargs)
        with patch("wind_map.publication.git",side_effect=racing):
            result=publication.publish(self.snapshot,self.clone,expected_remote=str(self.remote))
        self.assertEqual(result["status"],"pushed")
        self.assertEqual(len(attempts),2)
        self.assertFalse(any("--force" in args for args in attempts))
        self.assertEqual((self.clone/"status.txt").read_text(),"other writer")


class AdditionalBoundaryTests(unittest.TestCase):
    def test_unknown_capacity_is_not_displayed_as_zero(self):
        state={"permit_id":"unknown","status":"review","capacity_known":False,"capacity_w":0,"allocations":[{"region_id":"KR","capacity_w":0,"unallocated":True}]}
        totals=build_totals([state],[],[])["review"]
        self.assertIsNone(totals[0]["display_capacity_w"])
        self.assertEqual(totals[0]["unknown_capacity_count"],1)
    def test_same_capacity_register_preserves_allocation_event(self):
        events=[fact("g","grant",capacity_w=60_000_000,is_initial=True),fact("r","registry_observation","2026-09-30",capacity_w=60_000_000)]
        state=permit_state(PERMIT,events,"2026-11-12",NOW)
        self.assertEqual(state["capacity_event_id"],"g")
        self.assertEqual(state["capacity_ebl_date"]["start"],"2026-05-13")
        events[-1]["capacity_w"]=80_000_000
        self.assertIsNone(permit_state(PERMIT,events,"2026-11-12",NOW)["capacity_ebl_date"])
    def test_conflicting_milestone_dates_preserve_both_sources(self):
        from wind_map.milestones import annotate_conflicts
        a={"id":"a","type":"development","status":"approved","permit_ids":["p"],"date":date_value("2026-05-01"),"evidence_id":"a"}
        b={**a,"id":"b","date":date_value("2026-05-02"),"evidence_id":"b"}
        rows=annotate_conflicts([a,b])
        self.assertTrue(all(r["conflict"] for r in rows))
        self.assertEqual({r["evidence_id"] for r in rows},{"a","b"})
    def test_administrative_polygon_and_hole(self):
        from wind_map.geography import contains_point
        geometry={"type":"Polygon","coordinates":[[[128,35],[130,35],[130,37],[128,37],[128,35]],[[128.5,35.5],[129.5,35.5],[129.5,36.5],[128.5,36.5],[128.5,35.5]]]}
        self.assertTrue(contains_point(geometry,128.2,36))
        self.assertFalse(contains_point(geometry,129,36))
        self.assertFalse(contains_point(geometry,131,36))
    def test_search_refresh_archives_previous_completed_pages(self):
        with tempfile.TemporaryDirectory() as tmp, Store(tmp) as store:
            q=SearchQueue(store);ident=q.enqueue("s","풍력","2026-01-01","2026-11-12")
            job=q.claim()
            q.checkpoint(job,{"items":[{"id":"a"}],"total":1,"last_page":True,"complete_proof":"total"},now="2026-11-12T00:00:00Z")
            self.assertFalse(q.refresh(ident,now="2026-11-12T00:10:00Z"))
            self.assertTrue(q.refresh(ident,now="2026-11-12T02:00:00Z"))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0],1)
            self.assertEqual(q.claim()["cursor"],1)
    def test_worker_only_claims_configured_sources(self):
        with tempfile.TemporaryDirectory() as tmp, Store(tmp) as store:
            q=SearchQueue(store);q.enqueue("a","풍력","2026-01-01","2026-11-12")
            self.assertIsNone(q.claim(source_ids=["b"]))
            self.assertIsNotNone(q.claim(source_ids=["a"]))
    def test_cycle_enqueues_attachment_candidates_without_publishing(self):
        from wind_map.cycle import collection_cycle
        with tempfile.TemporaryDirectory() as tmp, Store(tmp) as store:
            configs={"s":{"kind":"korec","hosts":["example.com"],"agency":"test","watch_queries":["풍력"],"window_mode":"full_archive","collect_attachments":True}}
            def fetcher(config,job):
                page={"items":[{"id":"notice","title":"풍력 공고","url":"https://example.com/notice"}],"total":1,"last_page":True,"complete_proof":"source_total"}
                return page,{"content":b"test HTML","url":"https://example.com/search","checked_at":NOW}
            result=collection_cycle(store,configs,max_jobs=1,fetcher=fetcher,now=NOW)
            self.assertEqual(result["publication"],"not_requested")
            detail=store.db.execute("SELECT data FROM queue WHERE query='https://example.com/notice'").fetchone()
            self.assertEqual(json.loads(detail[0])["kind"],"notice_detail")
    def test_ocr_missing_tool_is_explicit(self):
        from wind_map.extract import ocr_page
        with patch("shutil.which",return_value=None),self.assertRaises(WindError):
            ocr_page(None,"doc",1)
    def test_remote_verification_uses_served_bytes(self):
        from wind_map.publication import verify_remote
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)/"runtime") as store:
            seed(store);snap=build_snapshot(store,"2026-11-12",NOW)
            root=Path(tmp)/"public";write_snapshot(snap,root)
            class Client:
                def fetch(self,url):
                    return {"content":(root/url.removeprefix("https://example.com/site/")).read_bytes()}
            result=verify_remote("https://example.com/site/",expected_snapshot=snap["snapshot_id"],client=Client())
            self.assertEqual(result["status"],"verified")
            (root/"data/wind-map/versions"/snap["snapshot_id"]/"index.json").write_bytes(b"bad")
            with self.assertRaises(WindError):verify_remote("https://example.com/site/",client=Client())
