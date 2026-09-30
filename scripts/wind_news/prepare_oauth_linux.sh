#!/usr/bin/env bash
set -euo pipefail
source_file=$(realpath "$1")
container=bre-wind-news-news-service-1
# docker cp cannot write to this container's read-only root. Use its existing
# private writable tmpfs, under the container's unprivileged service user.
docker exec -i "$container" sh -c 'umask 077; cat > /tmp/bre-oauth-device.py' < "$source_file"
