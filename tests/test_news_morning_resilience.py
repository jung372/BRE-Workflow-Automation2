"""Reproduce a previous-day outage using the normal API, worker and ledger."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if not all(importlib.util.find_spec(name) for name in ('duckdb', 'polars', 'flask', 'jsonschema')):
    raise unittest.SkipTest('news service dependencies are not installed')

from news.schedule import KST
from news.service import create_app
from news.store import Store


class EmptyCollection:
    def __init__(self):
        self.calls = []

    def collect(self, *, since, until):
        self.calls.append(until)
        return {'articles': [], 'source_results': [{'source_id': 'fixture', 'status': 'success_zero', 'count': 0}]}


class Publisher:
    def publish(self, issues):
        return {'state': 'WEB_VERIFIED', 'commit_sha': 'test', 'snapshot_id': 'test'}


class MorningResilienceTests(unittest.TestCase):
    def test_recovery_prioritizes_the_next_edition_and_sends_on_its_first_slot(self):
        for industry in ('wind', 'solar'):
            with self.subTest(industry=industry), TemporaryDirectory() as tmp:
                store = Store(Path(tmp) / 'news.duckdb')
                try:
                    self.check_recovery(industry, tmp, store)
                finally:
                    store.close()

    def check_recovery(self, industry, tmp, store):
        now = datetime(2026, 11, 23, 8, 0, tzinfo=KST)
        before, today = '2026-11-23', '2026-11-24'
        token = 'fixture-token-used-only-for-morning-recovery-tests'
        config = {'industry': industry, 'collection': {'enabled': True, 'sources': [{'source_id': 'fixture', 'required': True}]},
                  'policy': {'auto_recover': True, 'automatic_publication': True, 'review_mode': 'codex_oauth',
                             'publisher': {'enabled': True, 'pages_base_url': 'https://example.org/'}}}
        collector = EmptyCollection()
        app = create_app(config, runtime=tmp, store=store, token=token, start_worker=False,
                         clock=lambda: now, collector=collector, publisher=Publisher())
        runner = app.extensions['wind_news_runner']
        client = app.test_client()
        headers = {'Authorization': 'Bearer ' + token}
        stamp = lambda: now.astimezone(KST).isoformat()

        with patch('news.store.utcnow', side_effect=stamp), patch('news.pipeline.utcnow', side_effect=stamp), patch('news.service.utcnow', side_effect=stamp):
            runner.collect('prior-cutoff')
            prepared = runner.pipeline.prepare({'issue_date': before})
            runner.publish({key: prepared[key] for key in ('issue_id', 'revision', 'approval_hash')})
            claim = store.claim_delivery(prepared['issue_id'], 1, runner.profile.channel)
            self.assertTrue(claim['claimed'])

            stale = [store.submit('reconcile', f'BRE-{industry.upper()}-05B-Reconcile:{i}', {}) for i in range(25)]
            now += timedelta(seconds=1)
            old_briefing = store.submit('briefing', 'morning-briefing-' + before + '-08-30', {'issue_date': before})
            self.assertEqual(store.next_job()['operation'], 'reconcile')
            queued_backup = store.submit('backup', 'manual-backup', {})

            now += timedelta(days=1)
            now = now.replace(hour=7, minute=35)
            store.recover(now)
            self.assertTrue(all(store.job(job['job_id'])['error_code'] == 'SCHEDULE_EXPIRED' for job in stale))
            self.assertEqual(store.job(old_briefing['job_id'])['result']['error_code'], 'BRIEFING_DATE_EXPIRED')

            for hour, minute in ((7, 35), (7, 40), (7, 50)):
                now = now.replace(hour=hour, minute=minute)
                runner.schedule_local(now)
                for _ in range(4):
                    if not runner.run_once():
                        break
            self.assertEqual(len(collector.calls), 2)
            readiness = client.get('/v1/briefings/readiness', headers=headers).json
            self.assertTrue(readiness['web_verified'], readiness)
            self.assertLess(datetime.fromisoformat(readiness['verified_at']).time(), datetime(2026, 11, 24, 8, 30).time())
            self.assertEqual(store.job(queued_backup['job_id'])['status'], 'QUEUED')

            now = now.replace(hour=8, minute=30)
            with patch.dict('os.environ', {industry.upper() + '_NEWS_TEAMS_WEBHOOK_URL': 'https://example.org/fixture'}), \
                    patch('news.delivery.send_card', return_value={'status': 'ACCEPTED', 'http_status': 202}) as send:
                response = client.post('/v1/briefings/check', headers=headers, json={'issue_date': today})
                self.assertEqual(response.status_code, 202, response.json)
                self.assertTrue(runner.run_once())
                self.assertEqual(store.job(response.json['job_id'])['result']['status'], 'ACCEPTED')
                self.assertEqual(client.get('/v1/briefings/readiness', headers=headers).json['first_slot_result'], 'ACCEPTED')
                self.assertEqual(send.call_args.args[0]['issue_date'], today)
                now += timedelta(minutes=5)
                client.post('/v1/briefings/check', headers=headers, json={'issue_date': today})
                runner.run_once()
                send.assert_called_once()
            self.assertEqual(store.daily_delivery(before, runner.profile.channel)['status'], 'UNKNOWN')
