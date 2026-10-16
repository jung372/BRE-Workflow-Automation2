import contextlib
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from scripts.solar_news import report


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        pass

    def read(self, limit):
        return json.dumps(self.value).encode()


class SolarOperatorTests(unittest.TestCase):
    def run_operator(self, mode, industry='solar', outcome='ACCEPTED', base='http://127.0.0.1:8091'):
        calls = []
        values = [{'industry': industry, 'status': 'ready'},
                  {'issue_date': '2026-10-16', 'teams_configured': True, 'card_format': 'adaptive'},
                  {'status': outcome, 'private_debug': 'must-not-print'}]
        def open_request(request, **kwargs):
            calls.append(request)
            return Response(values.pop(0))
        opener = Mock()
        opener.open.side_effect = open_request
        output = io.StringIO()
        with patch.dict(os.environ, {'SOLAR_NEWS_API_TOKEN': 'fixture-private-token-' * 3,
                                     'SOLAR_NEWS_SERVICE_URL': base}), \
                patch.object(report.urllib.request, 'build_opener', return_value=opener), \
                patch('sys.argv', ['report.py', '--mode', mode, '--date', '2026-10-16']), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = report.main()
        return code, calls, output.getvalue()

    def test_preview_never_configures_or_sends_and_redacts_response(self):
        code, calls, output = self.run_operator('preview')
        self.assertEqual(code, 0)
        self.assertTrue(calls[-1].full_url.endswith('/v1/deliveries/preview'))
        self.assertTrue(all('/send' not in c.full_url for c in calls))
        self.assertIn('PREVIEWED', output)
        self.assertNotIn('must-not-print', output)
        self.assertNotIn('fixture-private', output)

    def test_wrong_industry_or_remote_plain_http_never_posts(self):
        code, calls, output = self.run_operator('send', industry='wind')
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get_method(), 'GET')
        self.assertEqual(output.strip(), 'SOLAR_OPERATOR_REQUEST_FAILED')
        code, calls, _ = self.run_operator('send', base='http://example.kr:8091')
        self.assertEqual((code, len(calls)), (1, 0))

    def test_connection_test_is_explicit_and_unknown_is_failure(self):
        code, calls, output = self.run_operator('test', outcome='UNKNOWN')
        self.assertEqual(code, 1)
        self.assertTrue(calls[-1].full_url.endswith('/v1/deliveries/send'))
        self.assertEqual(json.loads(calls[-1].data)['message_type'], 'connection_test')
        self.assertIn('UNKNOWN', output)


if __name__ == '__main__':
    unittest.main()
