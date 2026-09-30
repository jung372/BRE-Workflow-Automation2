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
