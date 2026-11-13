"""Configure the solar Teams receiver from protected stdin."""
import argparse
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.wind_news.configure_teams import update_settings, run


def apply_settings(settings, release, webhook, card_format):
    compose = ["docker", "compose", "--project-name", "bre-solar-news",
               "--env-file", str(settings), "-f", str(release / "infra/solar-news/compose.yaml")]
    run(compose + ["config", "--quiet"])
    run(compose + ["up", "-d", "--no-build", "--no-deps", "solar-news-service"])
    container = "bre-solar-news-solar-news-service-1"
    for _ in range(30):
        if run(["docker", "inspect", "--format", "{{.State.Health.Status}}", container]) == "healthy":
            break
        time.sleep(2)
    else:
        raise RuntimeError("SOLAR_HEALTH_FAILED")
    run(["docker", "exec", "-i", container, "python", "-c",
         "import os,sys; sys.exit(0 if os.environ.get('SOLAR_NEWS_TEAMS_WEBHOOK_URL') == sys.stdin.read() "
         "and os.environ.get('SOLAR_NEWS_TEAMS_CARD_FORMAT') == sys.argv[1] else 1)", card_format],
        input=webhook)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path.home() / "bre-solar-news"
    parser.add_argument("--settings", type=Path, default=root / "settings/compose.env")
    parser.add_argument("--release", type=Path, default=root / "current")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--card-format", required=True, choices=["adaptive", "messagecard"])
    args = parser.parse_args()
    try:
        path = args.settings.resolve()
        if path == ROOT or ROOT in path.parents or args.settings.is_symlink():
            raise ValueError("SOLAR_SETTINGS_MUST_BE_PRIVATE")
        path.parent.mkdir(parents=True, exist_ok=True)
        webhook = sys.stdin.read(8194)
        webhook, changed = update_settings(path, webhook, industry="solar", card_format=args.card_format)
        if args.apply:
            apply_settings(path, args.release, webhook, args.card_format)
        print("SOLAR_TEAMS_CONFIGURED changed=" + str(changed).lower() + " card_format=" + args.card_format)
        return 0
    except Exception:
        # Even parser/filesystem exceptions can include secrets or private paths.
        print("SOLAR_TEAMS_CONFIGURATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
