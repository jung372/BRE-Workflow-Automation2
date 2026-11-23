"""Exercise Windows recovery with mock tasks/WSL; never touch the local server."""
import base64
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class MorningGuardTests(unittest.TestCase):
    def run_guard(self, at, stopped="", supervisor="Running", unhealthy='', repeat=1, probe_ready=True):
        script = str(Path(__file__).resolve().parents[1] / "scripts/wind_news/ensure_morning_server.ps1").replace("'", "''")
        command = """
$env:COMPUTERNAME = 'DESKTOP-EVU6USL'
$global:started = @()
$global:restarted = @()
function Get-ScheduledTask { [pscustomobject]@{TaskName='Personal n8n Supervisor';State='SUPERVISOR'} }
function Start-ScheduledTask { param($TaskName) $global:started += 'supervisor' }
function wsl {
    $global:LASTEXITCODE=0
    if ($args -contains 'info') { return 'fixture-docker' }
    if ($args -contains 'inspect') {
        if ($args -contains '{{.State.Health.Status}}') {
            if ($args[-1] -eq 'UNHEALTHY') { return 'unhealthy' }
            return 'healthy'
        }
        if ($args[-1] -eq 'STOPPED') { return 'false' }
        return 'true'
    }
    if ($args -contains 'start') { $global:started += $args[-1]; return }
    if ($args -contains 'restart') { $global:restarted += $args[-1]; return }
    if ($args -contains 'python') { return '{"stage":"PREPARE_RUNNING","web_verified":PROBE_READY}' }
    throw 'UNEXPECTED_WSL_COMMAND'
}
for ($i=0; $i -lt REPEAT; $i++) {
    try { & 'SCRIPT' -StateDirectory 'STATE_DIR' -NowUtc ([DateTime]::Parse('WHEN_TIME').ToUniversalTime()) }
    catch {
        if ($_.Exception.Message -notlike 'MORNING_SERVICE_NOT_HEALTHY*') { throw }
        Write-Output $_.Exception.Message
    }
}
Write-Output ('STARTED=' + ($global:started -join ','))
Write-Output ('RESTARTED=' + ($global:restarted -join ','))
"""
        with TemporaryDirectory() as state:
            values = {'SUPERVISOR': supervisor, 'STOPPED': stopped, 'UNHEALTHY': unhealthy, 'SCRIPT': script,
                      'STATE_DIR': state.replace("'", "''"), 'WHEN_TIME': at, 'REPEAT': str(repeat),
                      'PROBE_READY': str(probe_ready).lower()}
            for old, new in values.items():
                command = command.replace(old, new)
            encoded = base64.b64encode(command.encode("utf-16-le")).decode()
            result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_healthy_services_are_never_restarted(self):
        output = self.run_guard("2026-10-14T07:00:00+09:00")
        self.assertIn("MORNING_SERVICES_HEALTHY", output)
        self.assertIn("STARTED=\n", output)

    def test_only_stopped_news_and_supervisor_are_started(self):
        output = self.run_guard("2026-10-14T09:25:00+09:00", "bre-wind-news-news-service-1", "Ready")
        self.assertIn("STARTED=supervisor,bre-wind-news-news-service-1", output)

    def test_no_startup_or_docker_calls_outside_morning_window(self):
        for at in ("2026-10-14T06:59:00+09:00", "2026-10-14T09:30:00+09:00", "2026-10-14T15:30:00+09:00"):
            output = self.run_guard(at, "bre-wind-news-news-service-1", "Ready")
            self.assertIn("MORNING_GUARD_OUTSIDE_WINDOW", output)
            self.assertNotIn("MORNING_SERVICES_HEALTHY", output)

    def test_unhealthy_solar_restarts_only_solar_and_has_daily_limit(self):
        container = 'bre-solar-news-solar-news-service-1'
        output = self.run_guard('2026-10-14T07:00:00+09:00', unhealthy=container, repeat=3)
        self.assertIn(f'RESTARTED={container},{container}', output)
        self.assertIn('MORNING_NEWS_RESTART_LIMIT=solar', output)

    def test_0820_reports_incomplete_preparation_for_both_services(self):
        output = self.run_guard('2026-10-14T08:20:00+09:00', probe_ready=False)
        self.assertIn('08:20 KST wind PREPARE_RUNNING', output)
        self.assertIn('08:20 KST solar PREPARE_RUNNING', output)
