"""Solar contracts use fixtures, local DBs and mocked transports only."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from news.codex_summary import CodexSummary
from news.delivery import build_card
from news.locks import file_lock
from news.pipeline import classify
from news.profiles import bind_config, profile_for
from news.publisher import Publisher, build_snapshot
from news.public_sources import parse_article
from news.relevance import industry_relevant
from news.service import create_app, load_config
from news.store import Store
from scripts.wind_news.build_workflows import build
from scripts.wind_news.configure_teams import update_settings
from test_wind_news_codex import FakeCodex
from test_wind_news_publisher import issue as wind_issue
import test_wind_news_publisher as publisher_fixtures
from test_wind_news_service import CONFIG, TOKEN

ROOT = Path(__file__).resolve().parents[1]


def solar_fixture():
    fixture = json.loads((ROOT / 'tests/fixtures/wind_news/articles.json').read_text(encoding='utf-8'))
    for a in fixture['articles']:
        for key in ('title', 'description'):
            a[key] = a[key].replace('해상풍력', '지붕 태양광')
        a['access_status'] = 'free'
    return fixture


def solar_issue():
    value = wind_issue()
    value['issue_id'] = 'solar-' + value['issue_date']
    value['items'][0].update(headline='지붕 태양광 EPC', solar_type='지붕형')
    return value


class SolarContracts(unittest.TestCase):
    def test_profile_defaults_and_efficiency_are_explicit(self):
        config = load_config(industry='solar')
        bind_config(config, 'solar')
        summary = config['policy']['summarizer']
        self.assertEqual((summary['model'], summary['review_model']), ('gpt-6.1-sol', 'gpt-6.1-sol'))
        self.assertEqual((summary['reasoning_effort'], summary['review_reasoning_effort']), ('low', 'medium'))
        self.assertFalse(config['collection']['enabled'])
        self.assertFalse(config['policy']['publisher']['enabled'])
        with self.assertRaises(ValueError):
            profile_for('../../wind')
        config['policy']['publisher']['industry'] = 'wind'
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            bind_config(config, 'solar')

    def test_solar_relevance_and_classification_do_not_admit_other_industries(self):
        for title in ('영농형 태양광 인허가', '태양광 PPA 계약', '폴리실리콘 전지 공급망'):
            self.assertTrue(industry_relevant(title, industry='solar'))
        for title in ('해상풍력 EPC', '태양열 난방', '기업 주가 상승', '반도체 웨이퍼 투자', '태양 흑점 관측'):
            self.assertFalse(industry_relevant(title, industry='solar'))
        a = classify({'title': '지붕 태양광 PPA 계약'}, 'solar')
        self.assertEqual(a['solar_type'], '지붕형')
        self.assertEqual(a['primary_category'], '전력시장·계약')
        self.assertNotIn('wind_type', a)
        self.assertEqual(classify({'title': '태양광 공급계약을 해지했다'}, 'solar')['contract_stage'], '해지·변경')

    def test_parser_uses_solar_profile_and_keeps_paywall_boundary(self):
        source = {'source_id': 'fixture', 'name': '시험', 'industry': 'solar', 'body_selector': 'article'}
        body = '지붕 태양광 발전소가 준공되어 운영을 시작했다. ' * 10
        html = '<meta property="og:title" content="태양광 발전소 준공"><meta property="article:published_time" content="2026-09-29T10:00:00+09:00"><article>' + body + '</article>'
        a = parse_article(html, source, 'https://example.kr/article')
        self.assertEqual(a['access_status'], 'free')
        self.assertIsNone(parse_article(html, {**source, 'industry': 'wind'}, 'https://example.kr/article'))

    def test_card_has_five_solar_titles_and_dated_solar_link(self):
        issue = solar_issue()
        issue['items'] *= 8
        for fmt in ('adaptive', 'messagecard'):
            rendered = json.dumps(build_card(issue, 'https://example.kr/', fmt, industry='solar'), ensure_ascii=False)
            self.assertIn('태양광 일간 시황', rendered)
            self.assertIn('/#/solar/2026-09-30', rendered)
            self.assertNotIn('/#/daily/', rendered)
            self.assertNotIn('6.', rendered)
            self.assertIn('5.', rendered)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            build_card(issue, 'https://example.kr/')

    def test_snapshots_separate_paths_and_reject_cross_industry_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            wind = build_snapshot([wind_issue()], folder)
            solar = build_snapshot([solar_issue()], folder, 'solar')
            before = (Path(folder) / wind['latest']['path']).read_bytes()
            self.assertTrue(all(p.startswith('data/solar-news/') for p in solar['files']))
            with self.assertRaisesRegex(ValueError, 'INDUSTRY'):
                build_snapshot([wind_issue()], folder, 'solar')
            self.assertEqual((Path(folder) / wind['latest']['path']).read_bytes(), before)

    def test_solar_setting_writer_is_dedicated_atomic_and_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings'
            webhook = 'https://fixture.logic.azure.com/workflows/test?sig=fake$only'
            _, changed = update_settings(path, '\ufeff' + webhook, industry='solar', card_format='adaptive')
            self.assertTrue(changed)
            self.assertIn("SOLAR_NEWS_TEAMS_WEBHOOK_URL='" + webhook + "'", path.read_text())
            self.assertNotIn('WIND_NEWS_', path.read_text())
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                update_settings(path, 'https://evil.example/secret', industry='solar')
            self.assertEqual(path.read_bytes(), before)
            path.write_text('WIND_NEWS_TEAMS_WEBHOOK_URL=fixture\n')
            with self.assertRaisesRegex(ValueError, 'DEDICATED'):
                update_settings(path, webhook, industry='solar')

    def test_lock_contention_and_exception_release(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / 'shared.lock'
            with file_lock(lock):
                with self.assertRaisesRegex(ValueError, 'LOCK_TIMEOUT'):
                    with file_lock(lock, timeout=.01):
                        self.fail('second publisher entered')
            with self.assertRaises(RuntimeError):
                with file_lock(lock):
                    raise RuntimeError('fixture')
            with file_lock(lock, timeout=.01):
                pass

    def test_solar_workflows_are_inactive_and_do_not_target_wind(self):
        flows = build(industry='solar')
        self.assertEqual(len(flows), 7)
        for flow in flows:
            self.assertFalse(flow['active'])
            self.assertEqual(flow['settings']['timezone'], 'Asia/Seoul')
            self.assertEqual(json.loads((ROOT / 'automation/n8n' / (flow['name'] + '.json')).read_text(encoding='utf-8')), flow)
            text = json.dumps(flow)
            self.assertNotIn('BRE-WIND', text)
            self.assertNotIn("'wind-'", text)
            self.assertNotIn('webhook', text.lower())
        deliver = next(f for f in flows if '-04-' in f['name'])
        self.assertIn('0-25/5 9 * * *', json.dumps(deliver))

    def test_solar_oauth_uses_sol_and_separate_private_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            auth, runtime = Path(folder) / 'auth', Path(folder) / 'runtime'
            auth.mkdir()
            fake = FakeCodex()
            fake.summary = {'summary': '지붕 태양광 발전소가 준공됐다.', 'evidence_quotes': ['지붕 태양광 발전소가 준공됐다.'], 'companies': [], 'project_name': None, 'region': None}
            cfg = load_config(industry='solar')['policy']['summarizer']
            article = {'title': '태양광 발전소 준공', 'description': fake.summary['summary']}
            with patch.dict(os.environ, {'SOLAR_NEWS_CODEX_HOME': str(auth), 'SOLAR_NEWS_RUNTIME_DIR': str(runtime)}):
                adapter = CodexSummary(cfg, runner=fake)
                self.assertTrue(adapter(article)['valid'])
                self.assertTrue(adapter(article)['valid'])
            calls = [c for c in fake.calls if c[0][1] == 'exec']
            self.assertEqual(len(calls), 2)  # independent review plus persistent cache
            self.assertTrue(all('gpt-6.1-sol' in args for args, _ in calls))
            self.assertIn('solar', calls[0][1]['input'])
            self.assertNotIn('TEAMS', str(calls[0][1]['env']))
            with patch.dict(os.environ, {'SOLAR_NEWS_CODEX_HOME': '', 'SOLAR_NEWS_RUNTIME_DIR': ''}):
                self.assertEqual(adapter(article)['fallback_reason'], 'CODEX_PRIVATE_RUNTIME_REQUIRED')


class SolarServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'solar.duckdb')
        config = copy.deepcopy(CONFIG)
        config['industry'] = 'solar'
        self.clock = datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)
        owner = self
        class LocalPublisher:
            def publish(self, issues):
                result = build_snapshot(issues, owner.root / 'site', 'solar')
                return {'state': 'WEB_VERIFIED', 'snapshot_id': result['snapshot_id'], 'descriptor': result['descriptor']}
        self.app = create_app(config, store=self.store, runtime=self.root, token=TOKEN, start_worker=False,
            publisher=LocalPublisher(), clock=lambda: self.clock,
            summarizer=lambda a, c: {'summary': a['description'], 'valid': True, 'review_required': False})
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer ' + TOKEN}
        self.pipeline = self.app.extensions['wind_news_pipeline']
        self.runner = self.app.extensions['wind_news_runner']

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def publish(self):
        self.pipeline.ingest(solar_fixture(), 'fixture')
        record = self.pipeline.prepare({'issue_date': '2026-09-30', 'batch_ids': ['fixture']})
        record = self.pipeline.review(record['issue_id'], {'revision': 1, 'approval_hash': record['content_hash'], 'action': 'approve', 'reason': 'fixture checked'})
        return self.runner.publish({'issue_id': record['issue_id'], 'revision': 1, 'approval_hash': record['approval_hash']})

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def test_ingest_publish_schema_review_and_no_wind_webhook_fallback(self):
        record = self.publish()
        self.assertEqual(record['issue_id'], 'solar-2026-09-30')
        self.assertEqual(record['state'], 'WEB_VERIFIED')
        self.assertEqual(record['issue']['items'][0]['solar_type'], '지붕형')
        self.assertNotIn('wind_type', record['issue']['items'][0])
        shell = self.client.get('/review').get_data(as_text=True)
        self.assertIn('태양광', shell)
        self.assertNotIn('/v1/issues/wind-', shell)
        with patch.dict(os.environ, {'SOLAR_NEWS_TEAMS_WEBHOOK_URL': '', 'WIND_NEWS_TEAMS_WEBHOOK_URL': 'fake-wind', 'TEAMS_WEBHOOK_URL': 'fake-shared'}), patch('news.delivery.send_card') as send:
            result = self.post('/v1/deliveries/send', {'issue_date': '2026-09-30'})
            self.assertEqual(result.json['error_code'], 'TEAMS_NOT_CONFIGURED')
            send.assert_not_called()
            preview = self.post('/v1/deliveries/preview', {'issue_date': '2026-09-30'})
            self.assertIn('/#/solar/', json.dumps(preview.json))
            self.assertEqual(self.store.pending_deliveries('solar-daily')[0]['issue_id'], record['issue_id'])

    def test_unknown_is_terminal_even_after_secret_rotation_and_channel_override(self):
        self.publish()
        with patch.dict(os.environ, {'SOLAR_NEWS_TEAMS_WEBHOOK_URL': 'fake-solar'}), patch('news.delivery.send_card', return_value={'status': 'UNKNOWN'}) as send:
            self.assertEqual(self.post('/v1/deliveries/send', {'issue_date': '2026-09-30'}).json['status'], 'UNKNOWN')
            with patch.dict(os.environ, {'SOLAR_NEWS_TEAMS_WEBHOOK_URL': 'fake-rotated'}):
                self.assertEqual(self.post('/v1/deliveries/send', {'issue_date': '2026-09-30'}).json['status'], 'UNKNOWN')
            self.assertEqual(self.post('/v1/deliveries/send', {'issue_date': '2026-09-30', 'channel_id': 'wind-news'}).status_code, 400)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[1], 'fake-solar')
            self.assertEqual(send.call_args.kwargs['industry'], 'solar')
            status = self.client.get('/v1/deliveries/status?issue_date=2026-09-30', headers=self.headers).json
            self.assertTrue(status['briefing_stopped'])

    def test_connection_test_does_not_consume_daily_claim(self):
        self.publish()
        with patch.dict(os.environ, {'SOLAR_NEWS_TEAMS_WEBHOOK_URL': 'fake-solar'}), patch('news.delivery.send_card', return_value={'status': 'ACCEPTED'}) as send:
            payload = {'issue_date': '2026-09-30', 'message_type': 'connection_test'}
            self.assertEqual(self.post('/v1/deliveries/send', payload).json['status'], 'ACCEPTED')
            self.assertTrue(send.call_args.kwargs['connection_test'])
            self.assertIsNone(self.store.daily_delivery('2026-09-30', 'solar-daily'))
            self.post('/v1/deliveries/send', payload)
            self.assertEqual(send.call_count, 1)
            self.post('/v1/deliveries/send', {'issue_date': '2026-09-30'})
            self.assertEqual(send.call_count, 2)

    def test_slot_idempotency_and_deadline(self):
        a = self.post('/v1/briefings/check', {}).json
        b = self.post('/v1/briefings/check', {}).json
        self.assertEqual(a['job_id'], b['job_id'])
        self.clock = datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(self.post('/v1/briefings/check', {}).json['error_code'], 'BRIEFING_DELIVERY_DEADLINE_MISSED')

    def test_database_cannot_be_reused_for_wind(self):
        with self.assertRaisesRegex(ValueError, 'industry_mismatch'):
            self.store.bind_industry('wind')


class SolarGitTests(unittest.TestCase):
    setUp = publisher_fixtures.GitPublisherTests.setUp
    tearDown = publisher_fixtures.GitPublisherTests.tearDown
    git = staticmethod(publisher_fixtures.GitPublisherTests.git)

    def test_two_publishers_and_remote_race_preserve_both_industries(self):
        lock = self.root / 'shared.lock'
        self.config['lock_path'] = str(lock)
        Publisher(self.config).publish([wind_issue()])
        solar_clone = self.root / 'solar-publish'
        self.git(self.root, 'clone', '--branch', 'main', str(self.remote), str(solar_clone))
        self.git(solar_clone, 'config', 'user.email', 'offline-test@example.invalid')
        self.git(solar_clone, 'config', 'user.name', 'Offline Test')
        (solar_clone / '.solar-news-publish-clone').touch()
        cfg = {**self.config, 'industry': 'solar', 'publish_clone': str(solar_clone), 'max_push_attempts': 3}
        raced = []
        def runner(clone, args, check=True):
            if args[0] == 'push' and not raced:
                raced.append(True)
                self.git(self.clone, 'fetch', 'origin', 'main')
                self.git(self.clone, 'merge', '--ff-only', 'origin/main')
                (self.clone / 'notice.txt').write_text('concurrent notice')
                self.git(self.clone, 'add', 'notice.txt')
                self.git(self.clone, 'commit', '-m', 'Notice update')
                self.git(self.clone, 'push', 'origin', 'main')
            return self.git(clone, *args, check=check)
        result = Publisher(cfg, git_runner=runner).publish([solar_issue()])
        self.assertEqual(result['state'], 'COMMITTED')
        self.assertTrue((solar_clone / 'data/wind-news/latest.json').exists())
        self.assertEqual((solar_clone / 'notice.txt').read_text(), 'concurrent notice')
        self.assertEqual((solar_clone / 'index.html').read_text(), 'existing notice site')
        Publisher(self.config).publish([wind_issue('2026-10-01')])
        self.assertTrue((self.clone / 'data/solar-news/latest.json').exists())
        with self.assertRaisesRegex(ValueError, 'ALLOWLIST'):
            Publisher(cfg)._stage_paths(solar_clone, ['data/wind-news/latest.json'])


if __name__ == '__main__':
    unittest.main()
