"""Create a non-secret solar service config; schedules and collection stay off."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from news.service import load_config
from news.profiles import bind_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = load_config(industry="solar")
    bind_config(config, "solar")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an operator's reviewed configuration.
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print("SOLAR_CONFIG_CREATED collection=false publisher=false")


if __name__ == "__main__":
    main()
