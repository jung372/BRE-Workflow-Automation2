import copy
import importlib.util
import itertools
import json
from pathlib import Path
import tempfile
import unittest

if not all(importlib.util.find_spec(x) for x in ("duckdb", "polars")):
    raise unittest.SkipTest("wind-news isolated dependencies are not installed")

from news.dedup import agreement_anchor, group_events
from news.pipeline import Pipeline, classify, event_key
from news.store import Store


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/wind_news/same_agreement_20261006.json").read_text(encoding="utf-8"))["articles"]


def classified(articles):
    for article in articles:
        article.update(classify(article))
        article["event_key"] = event_key(article, article)
        article["event_id"] = "evt-" + article["event_key"][:24]
    return articles


class AgreementDedupTests(unittest.TestCase):
    def test_four_reported_articles_have_one_event_in_every_input_order(self):
        articles = fixture()
        self.assertEqual(len({event_key(a, classify(a)) for a in articles}), 4)
        identities = set()
        for ordered in itertools.permutations(articles):
            groups = group_events(classified(copy.deepcopy(ordered)))
            self.assertEqual(len(groups), 1)
            event_id, members = next(iter(groups.items()))
            identities.add(event_id)
            self.assertEqual(len(members), 4)
            self.assertEqual({m["contract_stage"] for m in members}, {"MOU"})
            self.assertEqual({m["agreement_anchor"]["date"] for m in members}, {"2026-10-02"})
        self.assertEqual(len(identities), 1)

    def test_similar_topics_different_event_date_parties_scope_or_stage_stay_separate(self):
        original = fixture()[0]
        for before, after in [
            ("지난 2일", "지난 3일"),
            ("전남개발공사", "강원개발공사"),
            ("광주·전남지역", "강원지역"),
            ("업무협약", "EPC 본계약"),
            ("체결했다", "체결할 예정이다"),
            ("체결했다", "체결하지 않았다"),
            ("체결했다", "체결했다고 알려졌으나 취소했다"),
            ("육상풍력", "해상풍력"),
        ]:
            with self.subTest(after=after):
                other = {**original, "url": "https://example.kr/other",
                         "title": original["title"].replace(before, after),
                         "text": original["text"].replace(before, after)}
                self.assertEqual(len(group_events(classified([copy.deepcopy(original), other]))), 2)

    def test_missing_date_or_body_does_not_fall_back_to_title_similarity(self):
        for text in ("", "서부발전은 전남개발공사와 ‘육상풍력 활성화 및 상호협력을 위한 업무협약’을 체결했다."):
            self.assertIsNone(agreement_anchor({**fixture()[0], "text": text}))

    def test_main_stage_cannot_be_replaced_by_historical_agreement(self):
        for title in ("서부발전 전남개발공사 EPC 본계약 체결", "서부발전 전남개발공사 EPC 체결", "서부발전 전남개발공사 금융종결", "서부발전 전남개발공사 금융 종결", "서부발전 전남개발공사 협약 해지", "서부발전 전남개발공사 풍력 안전사고"):
            self.assertIsNone(agreement_anchor({**fixture()[0], "title": title}))

    def test_date_resolution_handles_year_rollover_and_rejects_invalid_or_future_date(self):
        article = {**fixture()[0], "source_published_at": "2027-01-02T09:00:00+09:00"}
        article["text"] = article["text"].replace("지난 2일", "지난 31일")
        self.assertEqual(agreement_anchor(article)["date"], "2026-12-31")
        for value in ("2026년 2월 30일", "2027년 1월 3일"):
            self.assertIsNone(agreement_anchor({**article, "text": article["text"].replace("지난 31일", value)}))

    def test_known_project_conflict_and_multiple_signings_do_not_merge(self):
        left, right = fixture()[:2]
        left["project_name"], right["project_name"] = "사업A", "사업B"
        self.assertEqual(len(group_events(classified([left, right]))), 2)
        article = fixture()[0]
        article["text"] += " " + article["text"].replace("지난 2일", "지난 3일")
        self.assertIsNone(agreement_anchor(article))

    def test_sparse_scope_does_not_transitively_bridge_different_regions(self):
        left, _, sparse, right = fixture()
        right["text"] = right["text"].replace("광주·전남지역", "강원지역")
        groups = group_events(classified([left, sparse, right]))
        self.assertEqual(sorted(len(m) for m in groups.values()), [1, 2])

    def test_pipeline_merges_before_summary_and_preserves_every_article_in_db(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "news.duckdb")
            try:
                collection = {"sources": [{"source_id": "fixture", "name": "시험매체", "discovery": "naver_news_search", "allow_body": True, "domestic": True, "language": "ko", "required": True}]}
                calls = []
                def summarize(article, cfg):
                    calls.append(article["article_id"])
                    return {"summary": article["title"], "valid": True, "review_required": False, "validation_status": "CODEX_VERIFIED"}
                pipeline = Pipeline(store, collection, {"automatic_publication": True, "review_mode": "codex_oauth", "require_rights_review": False, "max_summary_candidates": 1}, summarize)
                articles = [{**a, "source_id": "fixture"} for a in fixture()]
                pipeline.ingest({"articles": articles, "source_results": [{"source_id": "fixture", "status": "success", "count": 4}]}, "reported")
                result = pipeline.prepare({"issue_date": "2026-10-06", "batch_ids": ["reported"]})
                self.assertEqual(result["state"], "READY")
                self.assertEqual(len(calls), 1)
                counts = result["payload"]["counts"]
                self.assertEqual((counts["published_topics"], counts["merged_duplicates"], counts["deferred"]), (1, 3, 0))
                candidate = result["candidates"][0]
                self.assertEqual(len(candidate["member_article_ids"]), 4)
                self.assertEqual(store.call(lambda db: db.execute("SELECT count(*) FROM articles").fetchone()[0]), 4)
                self.assertEqual(store.call(lambda db: db.execute("SELECT count(*) FROM event_articles WHERE decision='dated_agreement_evidence'").fetchone()[0]), 4)
                replaced = pipeline.review(result["issue_id"], {"revision": result["revision"], "approval_hash": result["content_hash"], "action": "representative", "item_ids": [candidate["item"]["event_id"]], "article_id": candidate["member_article_ids"][1], "reason": "fixture representative review"})
                self.assertEqual(replaced["candidates"][0]["item"]["contract_stage"], "MOU")
                self.assertEqual(replaced["candidates"][0]["item"]["tags"], ["MOU"])
                self.assertTrue(replaced["candidates"][0]["held"])
                pipeline.review(result["issue_id"], {"revision": replaced["revision"], "approval_hash": replaced["content_hash"], "action": "approve", "reason": "fixture checked"})
                # Subsequent late re-discovery of the members does not republish.
                store.call(lambda db: db.execute("UPDATE issues SET state='COMMITTED'").fetchall(), write=True)
                following = pipeline.prepare({"issue_date": "2026-10-07", "batch_ids": ["reported"]})
                self.assertEqual(following["candidates"], [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
