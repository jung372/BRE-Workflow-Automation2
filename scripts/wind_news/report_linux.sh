#!/usr/bin/env bash
# GitHub Actions is the scheduler; secrets remain in the existing news service.
set -euo pipefail
source_root=$(realpath "$1")
mode=${2:-preview}
day=${3:-}
[[ "$mode" == preview || "$mode" == send || "$mode" == scheduled ]] || exit 2
[[ -z "$day" || "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 2
docker exec -i -e NEWS_REPORT_MODE="$mode" -e NEWS_REPORT_DATE="$day" \
    bre-wind-news-news-service-1 python - < "$source_root/scripts/wind_news/report_briefing.py"
