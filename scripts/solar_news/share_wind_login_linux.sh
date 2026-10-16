#!/usr/bin/env bash
# Reuse the existing private login in place. Never copy or print credentials.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
release_sha=$2
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
python3 "$source_root/scripts/solar_news/shared_login.py" preflight
# Both consumers must implement the same OS lock before sharing auth refresh.
bash "$source_root/scripts/wind_news/install_linux.sh" "$source_root" "$release_sha"
python3 "$source_root/scripts/solar_news/shared_login.py" configure
bash "$source_root/scripts/solar_news/install_linux.sh" "$source_root" "$release_sha"
python3 "$source_root/scripts/solar_news/shared_login.py" verify
