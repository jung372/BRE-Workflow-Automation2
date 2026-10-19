"""Teams report entrypoint; automatic sends only on weekdays before 09:00 KST."""
import argparse
import json
import os

from runtime_config import ensure_utf8_stdout, get_data_dir, get_runtime_dir
from logic.regular_report import run_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--manual", action="store_true", help="Explicit manual send outside the morning window")
    parser.add_argument("--force", action="store_true", help="Include an empty report; never bypass the daily claim")
    args = parser.parse_args()
    ensure_utf8_stdout()
    try:
        result = run_report(get_data_dir(), get_runtime_dir(), os.environ.get("TEAMS_WEBHOOK_URL", ""),
                            preview=args.preview, manual=args.manual,
                            force=args.force or os.environ.get("FORCE_SEND", "").lower() == "true")
    except Exception:
        # A crash during POST leaves the pre-written UNKNOWN claim intact.
        print("TEAMS_REPORT status=INTERNAL_ERROR")
        return 1
    print("TEAMS_REPORT " + json.dumps(result, ensure_ascii=False))
    if result["status"] == "DATA_NOT_READY":
        return 20
    return 1 if result["status"] in {"FAILED", "UNKNOWN", "NOT_CONFIGURED", "LEDGER_INVALID", "DEADLINE_MISSED"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
