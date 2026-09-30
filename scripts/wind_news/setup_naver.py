"""Interactive server-only Naver credential entry; never run in public CI."""
import getpass
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main():
    if os.environ.get("GITHUB_ACTIONS") or not sys.stdin.isatty():
        raise SystemExit("Run interactively on the server, outside GitHub Actions.")
    root = Path.home() / "bre-wind-news"
    env_file = root / "settings/compose.env"
    if not env_file.is_file():
        raise SystemExit("Install the news service first.")
    client_id = getpass.getpass("Naver Client ID (hidden): ").strip()
    secret = getpass.getpass("Naver Client Secret (hidden): ").strip()
    if not all(re.fullmatch(r"[A-Za-z0-9_-]{8,200}", item) for item in (client_id, secret)):
        raise SystemExit("Invalid credential format; nothing saved.")
    request = Request("https://openapi.naver.com/v1/search/news.json?query=wind&display=1",
                      headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": secret})
    try:
        with urlopen(request, timeout=20) as response:
            data = json.load(response)
        if not isinstance(data.get("items"), list):
            raise ValueError("unexpected response")
    except (HTTPError, URLError, ValueError):
        raise SystemExit("Naver news API validation failed; nothing saved.") from None
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                  if "=" in line and not line.startswith("#"))
    values.update(WIND_NEWS_NAVER_CLIENT_ID=client_id, WIND_NEWS_NAVER_CLIENT_SECRET=secret)
    temporary = env_file.with_suffix(".new")
    temporary.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    temporary.chmod(0o600)
    temporary.replace(env_file)
    print("Naver news API verified and saved privately. No credentials were printed.")
    print("Collection remains disabled until source policy and first publication checks are complete.")


if __name__ == "__main__":
    main()
