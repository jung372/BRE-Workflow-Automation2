"""Write only solar Teams settings from protected stdin; never send or restart."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.wind_news.configure_teams import update_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--card-format", required=True, choices=["adaptive", "messagecard"])
    args = parser.parse_args()
    try:
        path = args.settings.resolve()
        if path == ROOT or ROOT in path.parents or args.settings.is_symlink():
            raise ValueError("SOLAR_SETTINGS_MUST_BE_PRIVATE")
        path.parent.mkdir(parents=True, exist_ok=True)
        webhook = sys.stdin.read(8194)
        _, changed = update_settings(path, webhook, industry="solar", card_format=args.card_format)
        print("SOLAR_TEAMS_CONFIGURED changed=" + str(changed).lower() + " card_format=" + args.card_format)
        return 0
    except Exception:
        # Even parser/filesystem exceptions can include secrets or private paths.
        print("SOLAR_TEAMS_CONFIGURATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
