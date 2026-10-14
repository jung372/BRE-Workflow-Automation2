#!/usr/bin/env bash
# n8n owns scheduling; Actions uses the same service and delivery ledger.
set -euo pipefail
source_root=$(realpath "$1")
mode=${2:-preview}
day=${3:-}
[[ "$mode" == preview || "$mode" == send || "$mode" == scheduled || "$mode" == diagnose ]] || exit 2
[[ -z "$day" || "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || exit 2
client=report_briefing.py
[[ "$mode" != diagnose ]] || client=diagnose_briefing.py
docker exec -i -e NEWS_REPORT_MODE="$mode" -e NEWS_REPORT_DATE="$day" \
    bre-wind-news-news-service-1 python - < "$source_root/scripts/wind_news/$client"
