#!/usr/bin/env bash
# Explicit deployment of the morning briefing; no Teams message is sent here.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
release_sha=$2
root="$HOME/bre-wind-news"
container=bre-wind-news-news-service-1
# Obtain a verified service-managed DB backup before replacing the container.
docker exec -i "$container" python - <<'PY'
import hashlib, os, pathlib, time, uuid, requests
s=requests.Session(); s.trust_env=False
s.headers['Authorization']='Bearer '+os.environ['WIND_NEWS_API_TOKEN']
base='http://127.0.0.1:8090'
r=s.post(base+'/v1/backup', json={}, headers={'Idempotency-Key':'briefing-backup-'+uuid.uuid4().hex}, timeout=60)
r.raise_for_status(); job_id=r.json()['job_id']
for _ in range(60):
    r=s.get(base+'/v1/jobs/'+job_id, timeout=60); r.raise_for_status(); job=r.json()
    if job['status'] not in ('QUEUED','RUNNING'): break
    time.sleep(1)
assert job['status']=='SUCCEEDED', 'BRIEFING_BACKUP_FAILED'
path=pathlib.Path(os.environ['WIND_NEWS_RUNTIME_DIR'])/'backups'/job['result']['filename']
assert hashlib.sha256(path.read_bytes()).hexdigest()==job['result']['sha256']
print('BRIEFING_BACKUP_HASH_VERIFIED')
PY
bash "$source_root/scripts/wind_news/install_linux.sh" "$source_root" "$release_sha"
configured=$(docker exec "$container" python -c 'import os; print("true" if os.environ.get("WIND_NEWS_TEAMS_WEBHOOK_URL") else "false")')
[[ "$configured" == true || "$configured" == false ]] || exit 2
python3 "$source_root/scripts/wind_news/configure_briefing.py" --source "$source_root" --root "$root" --teams-configured "$configured"
bash "$source_root/scripts/wind_news/verify_linux.sh" "$source_root" "$release_sha"
