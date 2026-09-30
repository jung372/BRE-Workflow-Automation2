"""Short-lived device challenge only. Never reads the OAuth credential store.

`challenge` output is private: its caller captures it and uses encrypted Taildrop.
It must never be invoked directly in a public CI log.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def parse_challenge(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    url = re.search(r"https://auth\.openai\.com/codex/device\b", text)
    code = re.search(r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b", text)
    if not url or not code:
        return None
    return {"url": url.group(), "code": code.group()}


def main():
    action, session = sys.argv[1:]
    if action not in ("run", "challenge") or not re.fullmatch(r"[a-f0-9]{32}", session):
        raise SystemExit(2)
    path = Path("/tmp") / ("bre-oauth-" + session + ".log")
    if action == "challenge":
        if not path.exists():
            raise SystemExit(3)
        challenge = parse_challenge(path.read_text(errors="replace")[:16384])
        if not challenge:
            raise SystemExit(3)
        print(json.dumps(challenge))
        return
    os.umask(0o077)
    # A detached docker exec keeps the login alive after the CI job exits.
    try:
        with path.open("x") as log:
            subprocess.run(["codex", "login", "--device-auth"], stdout=log,
                           stderr=log, timeout=720, check=False)
    except subprocess.TimeoutExpired:
        pass
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
