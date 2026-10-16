import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.solar_news import shared_login


class SharedLoginTests(unittest.TestCase):
    def test_shared_mount_update_preserves_private_values_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth = root / 'wind-auth'
            auth.mkdir()
            settings = root / 'compose.env'
            settings.write_text('# private fixture\nSOLAR_NEWS_API_TOKEN=fixture=token\n'
                                'SOLAR_NEWS_CODEX_AUTH_ROOT=old\nSOLAR_NEWS_RELEASE=abc\n', encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                shared_login.update_auth_root(settings, auth)
                first = settings.read_bytes()
                shared_login.update_auth_root(settings, auth)
            self.assertEqual(first, settings.read_bytes())
            self.assertIn('SOLAR_NEWS_API_TOKEN=fixture=token\n', first.decode())
            self.assertIn('SOLAR_NEWS_RELEASE=abc\n', first.decode())
            self.assertEqual(first.decode().count('SOLAR_NEWS_CODEX_AUTH_ROOT='), 1)
            self.assertIn(str(auth.resolve()), first.decode())
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(list(auth.iterdir()), [])  # no credential reads/copies

    def test_foreign_settings_are_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / 'compose.env'
            for line in ('WIND_NEWS_API_TOKEN=fixture', 'TEAMS_WEBHOOK_URL=fixture'):
                settings.write_text(line, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'INDUSTRY_MISMATCH'):
                    shared_login.update_auth_root(settings, root)
                self.assertEqual(settings.read_text(encoding='utf-8'), line)

    def test_mount_validation_requires_writable_bind_at_exact_destination(self):
        mount = {'Destination':'/var/lib/bre-wind/codex-auth','Source':str(Path.cwd()),'Type':'bind','RW':True}
        with patch.object(shared_login, 'docker', return_value=json.dumps([mount])) as call:
            self.assertEqual(shared_login.auth_mount(shared_login.WIND,'wind'), Path.cwd().resolve())
            self.assertEqual(call.call_args.args[2], '{{json .Mounts}}')
        for change in ({'RW':False}, {'Type':'volume'}, {'Destination':'/other'}):
            with patch.object(shared_login, 'docker', return_value=json.dumps([{**mount, **change}])):
                with self.assertRaisesRegex(ValueError, 'MOUNT_INVALID'):
                    shared_login.auth_mount(shared_login.WIND,'wind')

    def test_cli_login_and_inference_fail_closed(self):
        with patch.object(shared_login,'docker',return_value='LOGIN_REQUIRED'):
            with self.assertRaisesRegex(ValueError,'EXISTING_CHATGPT_LOGIN_REQUIRED'):
                shared_login.check_login(shared_login.WIND,'wind')
        with patch.object(shared_login,'docker',return_value=json.dumps({'valid':False})):
            with self.assertRaisesRegex(ValueError,'TWO_PASS_FAILED'):
                shared_login.check_solar_model()


if __name__ == '__main__':
    unittest.main()
