#!/usr/bin/env bash
set -euo pipefail
umask 077
source_root=$(realpath "$1")
release_sha=$2
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
root="$HOME/bre-wind-news"
release="$root/releases/$release_sha"
network=n8n-personal_outbound
docker network inspect "$network" --format '{{.Name}}' >/dev/null
docker inspect n8n-personal-n8n-1 --format '{{.State.Running}}' | grep -qx true
mkdir -p "$release/infra/wind-news" "$release/config" "$root/settings" "$root/runtime" "$root/publish" "$root/codex-auth"
# Explicit allowlist: never copy credentials, existing runtime, or scraper data.
cp -a "$source_root/news" "$source_root/schemas" "$release/"
cp "$source_root/config/wind_news_collection.json" "$source_root/config/wind_news_policy.json" "$release/config/"
cp "$source_root/infra/wind-news/Dockerfile" "$source_root/infra/wind-news/compose.yaml" "$release/infra/wind-news/"
cp "$source_root/.dockerignore" "$release/"
mkdir -p "$release/automation" "$release/scripts"
cp -a "$source_root/automation/n8n" "$release/automation/"
cp -a "$source_root/scripts/wind_news" "$release/scripts/"
python3 - "$root" "$release" "$network" "$release_sha" <<'PY'
import json, os, secrets, sys
from pathlib import Path
root, release, network, sha = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
policy_path = root / 'settings' / 'config.json'
if not policy_path.exists():
    config = {name: json.loads((release / 'config' / f'wind_news_{name}.json').read_text()) for name in ('collection', 'policy')}
    policy_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
token_path = root / 'settings' / 'service-token'
if not token_path.exists():
    token_path.write_text(secrets.token_urlsafe(48))
# Private bootstrap output, never logged or uploaded as an Actions artifact.
env_path = root / 'settings' / 'compose.env'
previous = {}
if env_path.exists():
    previous = dict(line.split('=', 1) for line in env_path.read_text().splitlines() if '=' in line and not line.startswith('#'))
values = dict(previous, WIND_NEWS_RUNTIME_ROOT=str(root / 'runtime'), WIND_NEWS_PUBLISH_ROOT=str(root / 'publish'),
    WIND_NEWS_CONFIG_FILE=str(policy_path), WIND_NEWS_CODEX_AUTH_ROOT=str(root / 'codex-auth'),
    WIND_NEWS_DOCKER_NETWORK=network, WIND_NEWS_RELEASE=sha, WIND_NEWS_API_TOKEN=token_path.read_text().strip())
env_path.write_text(''.join(f'{k}={v}\n' for k,v in values.items()))
for path in (token_path, env_path, policy_path): os.chmod(path, 0o600)
PY
compose=(docker compose --project-name bre-wind-news --env-file "$root/settings/compose.env" -f "$release/infra/wind-news/compose.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" build
# Fix only these three private directories; container user owns its state.
docker run --rm --user 0 --entrypoint python \
    -v "$root/runtime:/state/runtime" -v "$root/publish:/state/publish" -v "$root/codex-auth:/state/codex-auth" \
    "bre-wind-news:$release_sha" -c 'import os; [os.chown("/state/"+p,10001,10001) for p in ("runtime","publish","codex-auth")]'
# Non-secret configuration must be readable by the unprivileged service.
chmod 644 "$root/settings/config.json"
"${compose[@]}" up -d --no-deps news-service
cid=$("${compose[@]}" ps -q news-service)
for attempt in $(seq 1 30); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$cid")
    [[ "$health" == healthy ]] && break
    sleep 2
done
[[ "$health" == healthy ]] || { echo 'NEWS_HEALTH_FAILED'; exit 1; }
ln -sfn "$release" "$root/current"
printf 'NEWS_SERVICE_READY release=%s\n' "$release_sha"
docker exec "$cid" codex --version
docker exec "$cid" python -c 'import duckdb,polars; print("DuckDB="+duckdb.__version__+" Polars="+polars.__version__)'
docker exec n8n-personal-n8n-1 n8n import:workflow --help
docker exec n8n-personal-n8n-1 n8n import:credentials --help
docker exec n8n-personal-n8n-1 n8n list:workflow --help
