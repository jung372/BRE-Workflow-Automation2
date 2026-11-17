import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from logic import runner
from scrapers.open_portal import MAINTENANCE_MESSAGE


class PortalMaintenanceStateTest(unittest.TestCase):
    def run_portal(self, current, error):
        saved = {
            'open_portal': {
                'daily_snapshots': {'2026-11-16': ['document-1']},
                'hourly_snapshots': {'2026-11-16 20': [
                    {'id': 'document-1', 'title': '풍력 허가', 'date': '2026-11-16'}
                ]},
            },
        }
        original = copy.deepcopy(saved)
        site = next(site for site in runner.SITES if site['id'] == 'open_portal')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'status.json'
            with (
                patch.object(runner, 'SITES', [site]),
                patch.object(runner, 'METMASTS', []),
                patch.object(runner, 'OUTPUT', str(output)),
                patch.object(runner, 'DATA_DIR', directory),
                patch.object(runner, 'sync_playwright'),
                patch.object(runner, 'load_state', return_value=saved),
                patch.object(runner, 'save_state') as save,
                patch.object(runner, 'fetch_site', return_value=(current, error)),
            ):
                result = runner.run()['sites'][0]
                self.assertEqual(json.loads(output.read_text())['sites'][0], result)
                return result, save.call_args.args[0], original

    def test_maintenance_preserves_successful_snapshots(self):
        result, saved, original = self.run_portal(None, MAINTENANCE_MESSAGE)
        self.assertEqual(saved, original)
        self.assertEqual(result['status'], 'maintenance')
        self.assertEqual(result['last_success_at'], '2026-11-16 20')
        self.assertTrue(result['error'])
        self.assertEqual(result['new_items'], [])

    def test_normal_response_resumes_without_maintenance_fields(self):
        result, _, _ = self.run_portal([], None)
        self.assertIsNone(result['error'])
        self.assertNotIn('status', result)
        self.assertNotIn('last_success_at', result)

    def test_other_failures_are_not_reported_as_maintenance(self):
        result, saved, original = self.run_portal(None, '정보목록 API HTTP 404')
        self.assertEqual(saved, original)
        self.assertNotIn('status', result)


if __name__ == '__main__':
    unittest.main()
