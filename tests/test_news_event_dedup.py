"""Same-event grouping and the shared two-day publication exclusion window."""
import copy
import importlib.util
import json
from pathlib import Path
import random
import tempfile
import unittest

if not all(importlib.util.find_spec(x) for x in ('duckdb', 'polars')):
    raise unittest.SkipTest('news isolated dependencies are not installed')

from news.dedup import event_anchor, group_events, same_event
from news.municipalities import headline_municipalities
from news.pipeline import Pipeline, classify, event_key, normalize_url
from news.store import Store, canonical


def wind_reports():
    path = Path(__file__).parent / 'fixtures/wind_news/same_cooperation_20261116.json'
    return json.loads(path.read_text(encoding='utf-8'))['articles']


def solar_reports():
    return [dict(title=title, url=f'https://example.kr/solar/{i}',
                 source_name=f'시험매체 {i}', source_published_at='2026-11-13T10:00:00+09:00',
                 text='새빛에너지는 지난 12일 초록건설과 전남 해뜰 수상 태양광 사업의 기술교류회 및 경영진 간담회를 열었다. 50MW 규모 발전소의 협력 방안을 논의했다.')
            for i, title in enumerate(('새빛에너지, 전남 50㎿ 수상 태양광 협력 강화',
                                       '새빛에너지·초록건설, 해뜰 수상 태양광 사업 추진 논의',
                                       '전남 태양광 기술교류회 연 새빛에너지와 초록건설'))]


def classified(articles, industry='wind'):
    for article in articles:
        article.update(classify(article, industry))
        article['event_key'] = event_key(article, article)
        article['event_id'] = 'evt-' + article['event_key'][:24]
    return articles


class NewsEventDedupTests(unittest.TestCase):
    def test_ten_ulsan_reports_merge_without_discovery_metadata_in_any_order(self):
        identities = set()
        for seed in range(10):
            articles = wind_reports()
            random.Random(seed).shuffle(articles)
            groups = group_events(classified(articles))
            self.assertEqual(len(groups), 1)
            event_id, members = next(iter(groups.items()))
            self.assertEqual(len(members), 10)
            identities.add(event_id)
        self.assertEqual(len(identities), 1)

    def test_november_24_wind_edition_groups_forum_and_ministry_event(self):
        path = Path(__file__).parent / 'fixtures/wind_news/same_forum_20261124.json'
        articles = json.loads(path.read_text(encoding='utf-8'))
        indices = {article["url"]: i for i, article in enumerate(articles, 1)}
        groups = group_events(classified(articles))
        membership = {frozenset(indices[a["url"]] for a in group) for group in groups.values()}
        self.assertEqual(membership, {frozenset(group) for group in (
            (2, 6, 8, 9, 10, 11, 12, 13), (3, 4), (1,), (5,), (7,), (14,), (15,),
        )})
        later = dict(articles[1], description=articles[1]["description"].replace("23일", "24일"))
        later.pop("news_anchor")
        self.assertFalse(same_event(articles[1], later))

    def test_municipality_mentions_and_article_url_identity(self):
        self.assertEqual(headline_municipalities("울산광역시와 군산시, 공동 사업"), {"울산시", "군산시"})
        self.assertEqual(headline_municipalities("울산시청, 공고"), {"울산시"})
        self.assertEqual(headline_municipalities("울산 앞바다 — 울산매일·울산시민"), set())
        self.assertEqual(normalize_url("https://example.kr/news?id=123&ref=naver&wlog_tag3=naver", ["example.kr"]),
                         "https://example.kr/news?id=123")

    def test_solar_reports_share_the_same_event_grouping(self):
        groups = group_events(classified(solar_reports(), 'solar'), 'solar')
        self.assertEqual([len(members) for members in groups.values()], [3])

    def test_units_legal_names_and_historical_mou_do_not_split_a_meeting(self):
        original = wind_reports()[1]
        other = copy.deepcopy(original)
        other.update(url='https://example.kr/new', title=original['title'].replace('375MW', '0.375GW'))
        other['text'] = other['text'].replace('한국동서발전', '동서발전㈜')
        self.assertEqual(len(group_events(classified([original, other]))), 1)
        self.assertEqual(event_anchor(original)['kind'], 'cooperation')
        self.assertEqual(event_anchor(original)['date'], '2026-11-12')

    def test_distinct_projects_parties_dates_capacity_stages_and_negation_stay_separate(self):
        original = wind_reports()[4]
        cases = [
            ('이스트블루파워', '새바람파워'), ('375', '500'),
            ('지난 12일', '지난 11일'), ('금양그린파워', '새빛에너지'),
            ('울산', '인천'),
        ]
        for before, after in cases:
            other = {**original, 'url': 'https://example.kr/other',
                     'title': original['title'].replace(before, after),
                     'text': original['text'].replace(before, after)}
            with self.subTest(after=after):
                self.assertEqual(len(group_events(classified([copy.deepcopy(original), other]))), 2)
        for stage in ('EPC 본계약 체결', '금융종결', '착공', '준공', '화재 발생', '간담회 개최 예정', '협력 논의 불발'):
            other = {**original, 'url': 'https://example.kr/milestone',
                     'title': f'동서발전 울산 375MW 이스트블루파워 부유식 해상풍력 {stage}'}
            with self.subTest(stage=stage):
                self.assertFalse(same_event(original, other))

    def test_common_industry_or_company_alone_is_not_a_duplicate(self):
        original = wind_reports()[1]
        for title in ('동서발전 재생에너지 투자 전략 발표', '울산 부유식 해상풍력 시장 전망',
                      '새빛에너지, 제주 해상풍력 협력 논의'):
            other = {**original, 'title': title, 'text': title, 'url': 'https://example.kr/unrelated'}
            self.assertFalse(same_event(original, other))

    def test_sparse_article_cannot_bridge_two_known_projects(self):
        left = wind_reports()[4]
        sparse = wind_reports()[9]
        right = {**left, 'url': 'https://example.kr/other', 'source_published_at': '2026-11-13T09:00:00+00:00',
                 'text': left['text'].replace('이스트블루파워', '새바람파워')}
        groups = group_events(classified([left, sparse, right]))
        self.assertEqual(sorted(len(members) for members in groups.values()), [1, 2])


class PublicationHistoryDedupTests(unittest.TestCase):
    def pipeline(self, path, industry):
        store = Store(path)
        self.addCleanup(store.close)
        calls = []
        def summarize(article, config):
            calls.append(article['article_id'])
            return dict(summary=article['title'], valid=True, review_required=False, validation_status='CODEX_VERIFIED')
        collection = {'industry': industry, 'sources': [dict(source_id='fixture', name='시험매체',
            discovery='naver_news_search', allow_body=True, domestic=True, language='ko', required=True)]}
        policy = dict(industry=industry, automatic_publication=True, review_mode='codex_oauth',
                      require_rights_review=False, max_summary_candidates=1)
        return Pipeline(store, collection, policy, summarize), calls

    def prepare(self, pipeline, articles, day, batch):
        payload = {'articles': [dict(a, source_id='fixture') for a in articles],
                   'source_results': [dict(source_id='fixture', status='success', count=len(articles))]}
        pipeline.ingest(payload, batch)
        return pipeline.prepare(dict(issue_date=day, batch_ids=[batch]))

    def test_grouping_precedes_summary_budget_and_preserves_every_member_in_both_industries(self):
        for industry, articles in (('wind', wind_reports()), ('solar', solar_reports())):
            with self.subTest(industry=industry), tempfile.TemporaryDirectory() as temp:
                pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', industry)
                result = self.prepare(pipeline, articles, '2026-11-16', 'reports')
                self.assertEqual(result['state'], 'READY')
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(result['payload']['items']), 1)
                self.assertEqual(result['payload']['counts']['merged_duplicates'], len(articles) - 1)
                self.assertEqual(len(result['candidates'][0]['member_article_ids']), len(articles))

    def test_recent_published_event_excludes_new_titles_and_urls_for_two_days_only(self):
        for industry, articles in (('wind', wind_reports()), ('solar', solar_reports())):
            for days in (1, 2, 3):
                with self.subTest(industry=industry, days=days), tempfile.TemporaryDirectory() as temp:
                    pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', industry)
                    original = self.prepare(pipeline, [articles[1]], '2026-11-14', 'original')
                    pipeline.store.call(lambda db: db.execute("UPDATE issues SET state='WEB_VERIFIED'").fetchall(), write=True)
                    duplicate = dict(articles[2], url='https://example.kr/new-report',
                                     source_published_at=f'2026-11-{13 + days:02d}T12:00:00+09:00')
                    result = self.prepare(pipeline, [duplicate], f'2026-11-{14 + days:02d}', 'new')
                    self.assertEqual(len(result['payload']['items']), int(days > 2))
                    self.assertEqual(len(calls), 1 + int(days > 2))
                    self.assertNotEqual(original['issue_id'], result['issue_id'])

    def test_unpublished_draft_does_not_exclude_following_report(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', 'wind')
            self.prepare(pipeline, [wind_reports()[1]], '2026-11-14', 'draft')
            result = self.prepare(pipeline, [wind_reports()[2]], '2026-11-15', 'new')
            self.assertEqual(len(result['payload']['items']), 1)
            self.assertEqual(len(calls), 2)

    def test_recent_history_uses_frozen_evidence_even_after_article_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', 'wind')
            self.prepare(pipeline, [wind_reports()[1]], '2026-11-14', 'original')
            pipeline.store.call(lambda db: db.execute("UPDATE issues SET state='WEB_VERIFIED'").fetchall(), write=True)
            changed = dict(wind_reports()[1], title='제주 풍력 신규 사업', text='제주 풍력 신규 사업')
            pipeline.ingest({'articles': [dict(changed, source_id='fixture')], 'source_results': []}, 'changed')
            result = self.prepare(pipeline, [wind_reports()[2]], '2026-11-15', 'new')
            self.assertEqual(result['payload']['items'], [])
            self.assertEqual(len(calls), 1)

    def test_recent_cooperation_does_not_hide_a_new_project_milestone(self):
        for industry, articles in (('wind', wind_reports()), ('solar', solar_reports())):
            with self.subTest(industry=industry), tempfile.TemporaryDirectory() as temp:
                pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', industry)
                self.prepare(pipeline, [articles[1]], '2026-11-14', 'original')
                pipeline.store.call(lambda db: db.execute("UPDATE issues SET state='WEB_VERIFIED'").fetchall(), write=True)
                new = dict(articles[1], title=articles[1]['title'] + ' EPC 본계약 체결',
                           url='https://example.kr/new-contract', source_published_at='2026-11-15T06:00:00+09:00')
                result = self.prepare(pipeline, [new], '2026-11-15', 'milestone')
                self.assertEqual(len(result['payload']['items']), 1)
                self.assertEqual(len(calls), 2)

    def test_actual_november_14_and_15_publications_exclude_all_ten_november_16_reports(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/wind_news/same_cooperation_20261116.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as temp:
            pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', 'wind')
            def history(db):
                for prior in fixture['recent_publications']:
                    db.execute('INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        [prior['issue_id'], 1, prior['issue_date'], 'WEB_VERIFIED', 'fixture', 'fixture',
                         canonical(prior), '[]', '[]', None, '2026-11-15T00:00:00Z', '2026-11-15T00:00:00Z'])
            pipeline.store.call(history, write=True)
            result = self.prepare(pipeline, fixture['articles'], '2026-11-16', 'reported')
            self.assertEqual(result['payload']['items'], [])
            self.assertEqual(result['candidates'], [])
            self.assertEqual(calls, [])

    def test_latest_published_correction_controls_the_recent_event_history(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', 'wind')
            original = self.prepare(pipeline, [wind_reports()[1]], '2026-11-14', 'original')
            empty = copy.deepcopy(original['payload'])
            empty.update(revision=2, items=[])
            def correct(db):
                db.execute("UPDATE issues SET state='WEB_VERIFIED'")
                db.execute("INSERT INTO issues SELECT issue_id,revision+1,issue_date,state,content_hash,approval_hash,?,'[]',batch_ids,publish_result,created_at,updated_at FROM issues WHERE issue_id=?", [canonical(empty), original['issue_id']])
            pipeline.store.call(correct, write=True)
            result = self.prepare(pipeline, [wind_reports()[2]], '2026-11-15', 'new')
            self.assertEqual(len(result['payload']['items']), 1)
            self.assertEqual(len(calls), 2)

    def test_same_day_municipality_limit_refills_and_applies_after_editor_approval(self):
        for industry, subject in (("wind", "해상풍력"), ("solar", "태양광")):
            with self.subTest(industry=industry), tempfile.TemporaryDirectory() as temp:
                pipeline, calls = self.pipeline(Path(temp) / 'news.duckdb', industry)
                pipeline.policy.update(max_items=2, max_summary_candidates=2)
                titles = [
                    f"울산시, {subject} 정책 공개",
                    f"울산광역시, {subject} 설치비 실태 조사",
                    f"군산시, {subject} 조례 개정",
                ]
                reports = [dict(title=title, description=title, url=f"https://example.kr/test/{i}",
                                source_published_at="2026-11-23T09:00:00+09:00")
                           for i, title in enumerate(titles)]
                issue = self.prepare(pipeline, reports, "2026-11-24", "municipalities")
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(issue["payload"]["items"]), 2)
                self.assertTrue(any(x["headline"] == titles[2] for x in issue["payload"]["items"]))
                approved = pipeline.review(issue["issue_id"], dict(
                    action="approve", reason="편집 검토", revision=issue["revision"], approval_hash=issue["content_hash"]))
                self.assertEqual(len(approved["payload"]["items"]), 2)
                self.assertEqual(sum(c.get("publication_exclusion", {}).get("reason") == "municipality_daily_limit"
                                     for c in approved["candidates"]), 1)


if __name__ == '__main__':
    unittest.main()
