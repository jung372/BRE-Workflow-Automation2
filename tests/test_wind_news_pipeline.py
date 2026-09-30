import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

if not all(importlib.util.find_spec(x) for x in ("duckdb", "polars")):
    raise unittest.SkipTest("wind-news isolated dependencies are not installed")

from news.pipeline import Pipeline, automatic_allowed, classify, normalize_url
from news.store import Conflict, Store, canonical

COLLECTION = {"sources": [{"source_id": "fixture", "name": "시험매체", "hosts": ["example.kr"], "enabled": True, "required": True, "domestic": True, "language": "ko", "rights_reviewed": True}]}
POLICY = {"automatic_publication": False, "require_rights_review": True, "max_items": 15}


def summary(article, config):
    return {"summary": "검증된 시험 요약: " + article["title"], "valid": True, "review_required": False}


class PipelineTests(unittest.TestCase):
    def test_headline_subject_wins_over_incidental_safety_reference(self):
        for title, expected in [
            ('전력망 더 짓기 전에 있는 망부터 사용', '인허가·정책'),
            ('해상풍력, 어민 보상 넘어 함께 돈 버는 바다', '민원·수용성'),
            ('해상풍력 작업 중 안전사고 발생', '사고·안전')]:
            self.assertEqual(classify({'title': title, 'text': '풍력 설비의 안전사고 가능성도 검토했다.'})['primary_category'], expected)

    def test_deep_body_mention_does_not_turn_project_into_accident(self):
        article = {'title':'풍력 발전 사업의 변화', 'text':'새로운 산업 동향을 소개한다. ' * 40 + '과거 안전사고에 대해서도 언급했다.'}
        self.assertEqual(classify(article)['primary_category'], '기타 주요 동향')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "news.duckdb")
        self.pipeline = Pipeline(self.store, copy.deepcopy(COLLECTION), copy.deepcopy(POLICY), summary)
        self.fixture = json.loads((Path(__file__).parent / "fixtures" / "wind_news" / "articles.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepare(self, batch="batch-1", payload=None):
        self.pipeline.ingest(payload or self.fixture, batch)
        return self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": [batch]})

    def approve(self, record, **extras):
        return self.pipeline.review(record["issue_id"], {"revision": record["revision"], "approval_hash": record["content_hash"], "action": "approve", "reason": "fixture evidence checked", **extras})

    def test_url_tracking_removed_article_query_preserved(self):
        self.assertEqual(normalize_url("https://example.kr/news?id=001&utm_campaign=a#fragment", ["example.kr"]), "https://example.kr/news?id=001")
        for url in ("http://example.kr/", "https://example.kr.evil/", "https://user:secret@example.kr/"):
            with self.assertRaises(ValueError):
                normalize_url(url, ["example.kr"])

    def test_paid_and_unverified_articles_are_excluded_before_storage_and_summary(self):
        self.pipeline.policy["require_free_access"] = True
        payload = copy.deepcopy(self.fixture)
        sample = payload["articles"][0]
        payload["articles"] = [dict(sample, url=f"https://example.kr/{number}", **flags)
            for number, flags in enumerate([{"access_status": "free"}, {"access_status": "paid"},
                {}, {"access_status": "free", "is_paywalled": True}, {"access_status": "free", "is_paywalled": "false"}])]
        calls = []
        self.pipeline.summarizer = lambda article, cfg: (calls.append(article) or summary(article, cfg))
        ingested = self.pipeline.ingest(payload, "access-filter")
        self.assertEqual((ingested["ingested"], ingested["excluded"]), (1, 4))
        self.assertEqual(self.store.call(lambda db: db.execute("SELECT count(*) FROM articles").fetchone()[0]), 1)
        self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["access-filter"]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["access_status"], "free")

    def test_enabling_free_only_also_filters_existing_unverified_batches(self):
        self.pipeline.ingest(self.fixture, "legacy")
        self.pipeline.policy["require_free_access"] = True
        self.pipeline.summarizer = lambda *args: self.fail("Unverified article reached LLM")
        record = self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["legacy"]})
        self.assertEqual(record["candidates"], [])

    def test_mou_epc_and_finance_stages_never_merge(self):
        result = self.prepare()
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual({c["item"]["contract_stage"] for c in result["candidates"]}, {"MOU", "본계약"})
        for title, stage in (("풍력 금융약정", "금융약정"), ("풍력 금융종결", "금융종결"), ("풍력 대출 실행", "실행")):
            self.assertEqual(classify({"title": title})["contract_stage"], stage)
        with self.assertRaises(ValueError):
            self.pipeline.review(result["issue_id"], {"revision": 1, "approval_hash": result["content_hash"], "action": "merge", "item_ids": [c["item"]["event_id"] for c in result["candidates"]], "reason": "cannot merge stages"})

    def test_default_draft_approval_and_edit_invalidates_old_hash(self):
        result = self.prepare()
        self.assertEqual(result["state"], "REVIEW_REQUIRED")
        self.assertEqual(result["payload"]["items"], [])
        approved = self.approve(result)
        self.assertEqual(approved["state"], "READY")
        self.assertEqual(len(approved["payload"]["items"]), 2)
        edited = self.pipeline.review(result["issue_id"], {"revision": 1, "approval_hash": approved["content_hash"], "action": "edit", "item_ids": [approved["candidates"][0]["item"]["event_id"]], "changes": {"summary": "편집자가 수정한 요약"}, "reason": "clarity"})
        self.assertIsNone(edited["approval_hash"])
        with self.assertRaises(Conflict):
            self.approve(approved)

    def test_coverage_failure_cannot_be_overridden_empty_success_is_reviewable(self):
        failed = self.prepare(payload={"articles": [], "source_results": [{"source_id": "fixture", "status": "parse_error", "count": 0}]})
        self.assertEqual(failed["state"], "BLOCKED")
        with self.assertRaises(Conflict):
            self.approve(failed)
        self.pipeline.ingest({"articles": [], "source_results": [{"source_id": "fixture", "status": "success_zero", "count": 0}]}, "healthy")
        recovered = self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["healthy"], "retry_blocked": True})
        self.assertEqual(recovered["revision"], 2)
        self.assertEqual(self.approve(recovered)["payload"]["content_status"], "no_news")

    def test_optional_failure_partial_required_saturation_blocks(self):
        self.pipeline.collection["sources"].append({**COLLECTION["sources"][0], "source_id": "optional", "required": False})
        payload = copy.deepcopy(self.fixture)
        payload["source_results"].append({"source_id": "optional", "status": "request_error", "count": 0})
        result = self.prepare(payload=payload)
        self.assertEqual(result["payload"]["content_status"], "partial")
        self.assertNotEqual(result["state"], "BLOCKED")

    def test_non_wind_result_is_excluded_without_downgrading_healthy_source(self):
        payload = copy.deepcopy(self.fixture)
        payload["articles"][0]["title"] = "태양광 소식"
        payload["articles"][0]["description"] = "태양광 사업 소식"
        payload["articles"][0].update(project_name=None, region=None, companies=[], event_date=None)
        result = self.prepare(payload=payload)
        self.assertNotEqual(result["state"], "BLOCKED")
        self.assertEqual(len(result["candidates"]), 1)

    def test_frozen_batch_does_not_take_later_article_content(self):
        self.pipeline.ingest(self.fixture, "frozen")
        changed = copy.deepcopy(self.fixture)
        changed["articles"][0]["title"] = "전남 새바람 해상풍력 MOU 체결 수정"
        self.pipeline.ingest(changed, "later")
        result = self.pipeline.prepare({"issue_date": "2026-09-30", "batch_ids": ["frozen"]})
        self.assertTrue(any("EPC 본계약" in c["item"]["headline"] for c in result["candidates"]))
        self.assertEqual(result["batch_ids"], ["frozen"])

    def test_exact_syndication_merges_and_can_split_and_change_representative(self):
        payload = copy.deepcopy(self.fixture)
        second = copy.deepcopy(payload["articles"][0])
        second.update(url="https://example.kr/news?id=003")
        payload["articles"] = [payload["articles"][0], second]
        result = self.prepare(payload=payload)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(len(candidate["member_article_ids"]), 2)
        changed = self.pipeline.review(result["issue_id"], {"revision": 1, "approval_hash": result["content_hash"], "action": "representative", "item_ids": [candidate["item"]["event_id"]], "article_id": candidate["member_article_ids"][-1], "reason": "source correction"})
        split = self.pipeline.review(result["issue_id"], {"revision": 1, "approval_hash": changed["content_hash"], "action": "split", "item_ids": [candidate["item"]["event_id"]], "reason": "different events confirmed"})
        self.assertEqual(len(split["candidates"]), 2)
        self.assertEqual(split["payload"]["items"], [])

    def test_cutoff_excludes_future_and_late_arrival_is_explicit(self):
        payload = copy.deepcopy(self.fixture)
        payload["articles"][0]["source_published_at"] = "2026-09-30T07:30:00+09:00"
        payload["articles"][1]["source_published_at"] = "2026-09-28T10:00:00+09:00"
        result = self.prepare(payload=payload)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["candidates"][0]["item"]["late_arrival"])

    def test_automatic_requires_measured_gate_not_boolean(self):
        self.assertFalse(automatic_allowed({"automatic_publication": True, "quality_gate": {"approved": True}}))
        self.assertTrue(automatic_allowed({"automatic_publication": True, "quality_gate": {"approved": True, "private_days": 7, "evaluated_article_pairs": 200, "evaluated_events": 50, "verified_representatives": 50, "merge_precision": .95, "merge_recall": .90, "stage_merge_errors": 0, "fact_match_rate": 1}}))

    def test_oauth_verified_sensitive_article_is_ready_without_manual_review(self):
        self.pipeline.policy.update(automatic_publication=True, review_mode="codex_oauth")
        self.pipeline.summarizer = lambda a, c: {"summary": "풍력 사고를 확인한 자체 요약", "valid": True, "validation_status": "CODEX_VERIFIED", "review_required": False, "metadata": {"companies": ["바람건설"], "project_name": "새바람", "region": "전남"}}
        payload = copy.deepcopy(self.fixture)
        payload["articles"][0]["title"] = "전남 새바람 해상풍력 사고 조사"
        result = self.prepare(payload=payload)
        self.assertEqual(result["state"], "READY")
        self.assertEqual(len(result["payload"]["items"]), 2)
        self.assertTrue(any(item["companies"] == ["바람건설"] for item in result["payload"]["items"]))

    def test_oauth_failure_is_held_partial_and_cannot_fabricate_no_news(self):
        self.pipeline.policy.update(automatic_publication=True, review_mode="codex_oauth")
        self.pipeline.summarizer = lambda a, c: {"summary": "", "valid": False, "review_required": True}
        result = self.prepare()
        self.assertEqual(result["state"], "READY")
        self.assertEqual(result["payload"]["content_status"], "partial")
        self.assertEqual(result["payload"]["counts"]["held"], 2)
        self.assertEqual(result["payload"]["items"], [])

    def test_oauth_hold_preserves_safe_operator_diagnostic(self):
        self.pipeline.policy.update(automatic_publication=True, review_mode="codex_oauth")
        self.pipeline.summarizer = lambda a, c: {"summary": "", "valid": False, "review_required": True, "fallback_reason": "CODEX_CHATGPT_LOGIN_REQUIRED"}
        result = self.prepare()
        self.assertEqual({c["reason"] for c in result["candidates"]}, {"CODEX_CHATGPT_LOGIN_REQUIRED"})
        self.assertNotIn("CODEX_CHATGPT_LOGIN_REQUIRED", canonical(result["payload"]))

    def test_oauth_never_accepts_valid_extractive_fallback_as_review(self):
        self.pipeline.policy.update(automatic_publication=True, review_mode="codex_oauth")
        self.pipeline.summarizer = lambda a, c: {"summary": a["title"], "valid": True, "review_required": False, "validation_status": "EXTRACTIVE_FALLBACK"}
        result = self.prepare()
        self.assertEqual(result["payload"]["items"], [])
        self.assertEqual(result["payload"]["counts"]["held"], 2)
        self.assertEqual(result["payload"]["content_status"], "partial")

    def test_old_published_metadata_kept_when_raw_evidence_purged(self):
        record = self.approve(self.prepare())
        self.store.call(lambda db: db.execute("UPDATE articles SET updated_at='2020-01-01T00:00:00+00:00'").fetchall(), write=True)
        self.assertEqual(self.pipeline.purge_expired_evidence(), 2)
        after = self.store.issue(record["issue_id"])
        self.assertEqual(after["payload"], record["payload"])
        for row in self.store.call(lambda db: db.execute("SELECT evidence FROM articles").fetchall()):
            self.assertNotIn("description", json.loads(row[0]))


if __name__ == "__main__":
    unittest.main()
