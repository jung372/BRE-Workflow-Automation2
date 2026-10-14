"""Connect news to report.yml's receiver using private stdin; print no secrets."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


def update_settings(path, webhook):
    webhook = webhook.strip()
    parsed = urlsplit(webhook)
    if (not webhook or len(webhook) > 8192 or any(c.isspace() or c in "'\\\"" for c in webhook)
            or parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or not parsed.hostname):
        raise ValueError("INVALID_SHARED_TEAMS_WEBHOOK")
    if not (any(parsed.hostname.endswith("." + suffix) for suffix in
                ("logic.azure.com", "api.powerplatform.com", "webhook.office.com"))
            or parsed.hostname == "outlook.office.com" and parsed.path.startswith("/webhook/")):
        raise ValueError("UNSUPPORTED_SHARED_TEAMS_HOST")
    original = path.read_text(encoding="utf-8")
    keys = {"WIND_NEWS_TEAMS_WEBHOOK_URL", "WIND_NEWS_TEAMS_CARD_FORMAT"}
    lines = [line for line in original.splitlines() if line.split("=", 1)[0].strip() not in keys]
    # Single quotes prevent Compose from interpolating any $ in the signed URL.
    lines += [f"WIND_NEWS_TEAMS_WEBHOOK_URL='{webhook}'", "WIND_NEWS_TEAMS_CARD_FORMAT=messagecard"]
    updated = "\n".join(lines) + "\n"
    changed = updated != original
    if changed:
        fd, temporary = tempfile.mkstemp(prefix=".teams-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(updated)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return webhook, changed


def run(args, **kwargs):
    # Compose and Docker errors may contain configuration: never relay output.
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, **kwargs)
    if result.returncode:
        raise RuntimeError("TEAMS_CONFIGURATION_COMMAND_FAILED")
    return result.stdout.strip()


def main():
    stage = "SERVICE_VERSION"
    try:
        root = Path.home() / "bre-wind-news"
        compose_file = root / "current" / "infra" / "wind-news" / "compose.yaml"
        if "WIND_NEWS_TEAMS_CARD_FORMAT" not in compose_file.read_text(encoding="utf-8"):
            raise RuntimeError("NEWS_SERVICE_UPGRADE_REQUIRED")
        stage = "SETTINGS_UPDATE"
        webhook, changed = update_settings(root / "settings" / "compose.env", sys.stdin.read(8193))
        compose = ["docker", "compose", "--project-name", "bre-wind-news", "--env-file",
                   str(root / "settings" / "compose.env"), "-f", str(compose_file)]
        stage = "COMPOSE_VALIDATE"
        run(compose + ["config", "--quiet"])
        stage = "CONTAINER_APPLY"
        run(compose + ["up", "-d", "--no-deps", "news-service"])
        container = "bre-wind-news-news-service-1"
        stage = "HEALTH_CHECK"
        for _ in range(30):
            if run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container]) == "healthy":
                break
            time.sleep(2)
        else:
            raise RuntimeError("NEWS_HEALTH_FAILED")
        stage = "CONFIG_EQUALITY"
        run(["docker", "exec", "-i", container, "python", "-c",
             "import os,sys; sys.exit(0 if os.environ.get('WIND_NEWS_TEAMS_WEBHOOK_URL') == sys.stdin.read() "
             "and os.environ.get('WIND_NEWS_TEAMS_CARD_FORMAT') == 'messagecard' else 1)"], input=webhook)
        print("SHARED_TEAMS_CONFIG_VERIFIED changed=" + str(changed).lower() + " card_format=messagecard")
        return 0
    except Exception as error:
        # Fixed labels only: exceptions and command output can contain secrets.
        known = {"INVALID_SHARED_TEAMS_WEBHOOK", "UNSUPPORTED_SHARED_TEAMS_HOST", "NEWS_SERVICE_UPGRADE_REQUIRED",
                 "TEAMS_CONFIGURATION_COMMAND_FAILED", "NEWS_HEALTH_FAILED"}
        code = str(error) if str(error) in known else type(error).__name__
        if code not in known | {"FileNotFoundError", "PermissionError", "TimeoutExpired", "ValueError"}:
            code = "UNEXPECTED_ERROR"
        print("SHARED_TEAMS_CONFIG_FAILED stage=" + stage + " code=" + code)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
