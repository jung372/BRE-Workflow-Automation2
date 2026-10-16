#!/usr/bin/env bash
# Install an isolated service. Collection, publishing and schedules stay off
# on first install; credentials are never copied from wind or printed.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
release_sha=$2
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
root="$HOME/bre-solar-news"
release="$root/releases/$release_sha"
network=n8n-personal_outbound
docker network inspect "$network" --format '{{.Name}}' >/dev/null
docker inspect n8n-personal-n8n-1 --format '{{.State.Running}}' | grep -qx true
mkdir -p "$release/infra/wind-news" "$release/infra/solar-news" "$release/config" "$release/scripts" \
    "$root/settings" "$root/runtime" "$root/publish" "$root/codex-auth" "$HOME/bre-news-locks"
cp -a "$source_root/news" "$source_root/schemas" "$release/"
cp "$source_root/config/solar_news_collection.json" "$source_root/config/solar_news_policy.json" "$release/config/"
cp "$source_root/infra/wind-news/Dockerfile" "$release/infra/wind-news/"
cp "$source_root/infra/solar-news/compose.yaml" "$release/infra/solar-news/"
cp -a "$source_root/scripts/solar_news" "$release/scripts/"
cp "$source_root/.dockerignore" "$release/"
python3 - "$root" "$release" "$network" <<'PY'
import json, os, secrets, sys
from pathlib import Path
root, release, network = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
policy_path = root / 'settings' / 'config.json'
if policy_path.exists():
    config = json.loads(policy_path.read_text())
    if config.get('industry') != 'solar':
        raise SystemExit('SOLAR_CONFIG_INDUSTRY_MISMATCH')
else:
    config = {name: json.loads((release / 'config' / f'solar_news_{name}.json').read_text()) for name in ('collection', 'policy')}
    config['industry'] = 'solar'
# The requested model change applies to both generation and independent review.
config['policy']['summarizer'].update(model='gpt-6.1-sol', review_model='gpt-6.1-sol')
temporary = policy_path.with_suffix('.tmp')
temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2))
os.chmod(temporary, 0o644)
os.replace(temporary, policy_path)
env_path = root / 'settings' / 'compose.env'
if not env_path.exists():
    values = dict(SOLAR_NEWS_RUNTIME_ROOT=str(root / 'runtime'), SOLAR_NEWS_PUBLISH_ROOT=str(root / 'publish'),
        SOLAR_NEWS_CONFIG_FILE=str(policy_path), SOLAR_NEWS_CODEX_AUTH_ROOT=str(root / 'codex-auth'),
        SOLAR_NEWS_DOCKER_NETWORK=network, SOLAR_NEWS_API_TOKEN=secrets.token_urlsafe(48),
        SOLAR_NEWS_RELEASE=release.name,
        BRE_NEWS_LOCK_ROOT=str(root.parent / 'bre-news-locks'))
    with env_path.open('x') as stream:
        stream.write(''.join(f'{key}={value}\n' for key, value in values.items()))
    os.chmod(env_path, 0o600)
else:
    # Preserve private settings verbatim; update only the non-secret image tag.
    lines = env_path.read_text().splitlines()
    lines = [line for line in lines if not line.startswith('SOLAR_NEWS_RELEASE=')]
    temporary = env_path.with_suffix('.tmp')
    temporary.write_text('\n'.join(lines + ['SOLAR_NEWS_RELEASE=' + release.name]) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, env_path)
PY
export SOLAR_NEWS_RELEASE="$release_sha"
compose=(docker compose --project-name bre-solar-news --env-file "$root/settings/compose.env" -f "$release/infra/solar-news/compose.yaml")
"${compose[@]}" config --quiet
"${compose[@]}" build
docker run --rm --user 0 --entrypoint python \
    -v "$root/runtime:/state/runtime" -v "$root/publish:/state/publish" \
    -v "$root/codex-auth:/state/codex-auth" -v "$HOME/bre-news-locks:/state/locks" \
    "bre-solar-news:$release_sha" -c 'import os; [os.chown("/state/"+p,10001,10001) for p in ("runtime","publish","codex-auth","locks")]'
"${compose[@]}" up -d --no-deps solar-news-service
cid=$("${compose[@]}" ps -q solar-news-service)
for attempt in $(seq 1 30); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$cid")
    [[ "$health" == healthy ]] && break
    sleep 2
done
[[ "$health" == healthy ]] || { echo 'SOLAR_HEALTH_FAILED'; exit 1; }
ln -sfn "$release" "$root/current"
printf 'SOLAR_SERVICE_READY release=%s\n' "$release_sha"
docker exec "$cid" python -c 'import json,os; from pathlib import Path; c=json.loads(Path(os.environ["SOLAR_NEWS_CONFIG"]).read_text()); s=c["policy"]["summarizer"]; print(json.dumps(dict(industry=c["industry"], model=s["model"], review_model=s["review_model"], collection_enabled=c["collection"]["enabled"], publisher_enabled=c["policy"]["publisher"]["enabled"], oauth_configured=(Path(os.environ["SOLAR_NEWS_CODEX_HOME"])/"auth.json").is_file(), teams_configured=bool(os.environ.get("SOLAR_NEWS_TEAMS_WEBHOOK_URL")), publish_connected=(Path(os.environ["SOLAR_NEWS_PUBLISH_CLONE"])/".git").is_dir())))'
