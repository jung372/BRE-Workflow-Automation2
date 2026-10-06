#!/usr/bin/env bash
# Only public configuration and selected runtime metadata. Never read .env/auth.
set -u
printf 'Linux identity: '; id
printf 'Tool locations:\n'
for tool in docker python3 node npm codex; do command -v "$tool" || true; done
printf 'Relevant service names:\n'
systemctl list-units --type=service --all --no-legend | awk '/docker|n8n|wind/ {print $1,$3,$4}'
printf 'Passwordless sudo: '
if sudo -n true 2>/dev/null; then echo yes; else echo no; fi
if command -v docker >/dev/null; then
    docker version --format '{{.Server.Version}}'
    docker ps --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}'
    docker ps --format '{{.Names}}' | while read -r name; do
        case "$name" in *n8n*)
            printf 'n8n mounts and networks: %s\n' "$name"
            docker inspect --format '{{json .Mounts}}' "$name"
            docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{$name}} {{end}}' "$name"
            docker exec "$name" n8n --version
        esac
    done
fi
printf 'Home project directory names:\n'
find "$HOME" -maxdepth 2 -type d -name '*n8n*' -print 2>/dev/null
printf 'News settings file metadata (contents are never read):\n'
python3 - <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import json
settings = Path.home() / 'bre-wind-news' / 'settings'
print('NEWS_SETTINGS_DIRECTORY=' + str(settings))
for name in ('compose.env', 'compose.env.txt', 'copose.env', 'copose.env.txt', '.env'):
    path = settings / name
    item = {'name': name, 'exists': path.is_file()}
    if item['exists']:
        stat = path.stat()
        item.update(bytes=stat.st_size, modified_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat())
    print('NEWS_SETTINGS_FILE=' + json.dumps(item))
PY
docker inspect bre-wind-news-news-service-1 --format 'NEWS_COMPOSE_FILES={{index .Config.Labels "com.docker.compose.project.config_files"}} NEWS_COMPOSE_DIRECTORY={{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true
