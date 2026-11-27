#!/usr/bin/env bash
set -euo pipefail

# Run from any directory; the virtual environment stays inside this checkout.
repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python3 -m venv .venv-cloud
.venv-cloud/bin/python -m pip install --disable-pip-version-check -r requirements.txt -r news/requirements.txt -r wind_map/requirements.txt
printf '%s\n' 'Setup complete. Activate with: source .venv-cloud/bin/activate'
