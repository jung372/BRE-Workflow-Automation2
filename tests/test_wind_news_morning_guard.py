"""Exercise Windows recovery with mock tasks/WSL; never touch the local server."""
import base64
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class MorningGuardTests(unittest.TestCase):
    def run_guard(self, at, stopped="", supervisor="Running"):
        script = str(Path(__file__).resolve().parents[1] / "scripts/wind_news/ensure_morning_server.ps1").replace("'", "''")
        command = """
$env:COMPUTERNAME = 'DESKTOP-EVU6USL'
$global:started = @()
function Get-ScheduledTask { [pscustomobject]@{TaskName='Personal n8n Supervisor';State='SUPERVISOR'} }
function Start-ScheduledTask { param($TaskName) $global:started += 'supervisor' }
function wsl {
    $global:LASTEXITCODE=0
    if ($args -contains 'info') { return 'fixture-docker' }
    if ($args -contains 'inspect') {
        if ($args -contains '{{.State.Health.Status}}') { return 'healthy' }
        if ($args[-1] -eq 'STOPPED') { return 'false' }
        return 'true'
    }
    if ($args -contains 'start') { $global:started += $args[-1]; return }
    throw 'UNEXPECTED_WSL_COMMAND'
}
& 'SCRIPT' -NowUtc ([DateTime]::Parse('AT').ToUniversalTime())
Write-Output ('STARTED=' + ($global:started -join ','))
""".replace("SUPERVISOR", supervisor).replace("STOPPED", stopped).replace("SCRIPT", script).replace("AT", at)
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
