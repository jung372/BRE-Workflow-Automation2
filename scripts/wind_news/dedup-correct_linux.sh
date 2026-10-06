#!/usr/bin/env bash
# Explicit correction of today's published, approved article set only.
set -euo pipefail
source_root=$(realpath "$1")
container=bre-wind-news-news-service-1
docker inspect "$container" --format 'IMAGE={{.Config.Image}} HEALTH={{.State.Health.Status}}'
docker exec -i "$container" python -u - < "$source_root/scripts/wind_news/correct_duplicates.py"
