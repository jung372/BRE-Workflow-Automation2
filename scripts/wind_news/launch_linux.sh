#!/usr/bin/env bash
# Explicit operator action: enable public-source collection and publish first issue.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
root="$HOME/bre-wind-news"
container=bre-wind-news-news-service-1
python3 - "$root/settings/config.json" "$source_root/config" <<'PY'
import json, sys
from pathlib import Path
path, source = map(Path, sys.argv[1:])
config = json.loads(path.read_text())
collection = json.loads((source / 'wind_news_collection.json').read_text())
assert collection['provider'] == 'public_publishers'
collection['enabled'] = True
config['collection'] = collection
config['policy'].update(require_rights_review=False, require_free_access=True, auto_recover=True,
    source_authorization_basis='user_instruction_2026-09-30_public_articles')
config['policy']['publisher']['enabled'] = True
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
PY
docker restart "$container" >/dev/null
for attempt in $(seq 1 30); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
    [[ "$health" == healthy ]] && break
    sleep 2
done
[[ "$health" == healthy ]] || { echo NEWS_HEALTH_FAILED; exit 1; }
docker exec -i "$container" python -u - <<'PY'
from datetime import datetime, timedelta, timezone
import json, os, re, time, uuid
import requests
s = requests.Session(); s.trust_env = False
s.headers['Authorization'] = 'Bearer ' + os.environ['WIND_NEWS_API_TOKEN']
base = 'http://127.0.0.1:8090'
def job(path, payload):
    response = s.post(base+path, json=payload, headers={'Idempotency-Key':'launch-'+uuid.uuid4().hex}, timeout=20)
    assert response.status_code == 202, 'LAUNCH_SUBMIT_FAILED'
    job_id = response.json()['job_id']
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        response = s.get(base+'/v1/jobs/'+job_id, timeout=15)
        response.raise_for_status()
        record = response.json()
        if record['status'] == 'SUCCEEDED': return record['result']
        if record['status'] == 'FAILED':
            code = str(record.get('error_code','UNKNOWN'))
            print('LAUNCH_ERROR='+ (code if re.fullmatch('[A-Z_]{1,80}', code) else 'JOB_FAILED'), flush=True)
            raise SystemExit(1)
        time.sleep(5)
    raise SystemExit('LAUNCH_JOB_TIMEOUT')
collected = job('/v1/collect', {})
print('LAUNCH_INGESTED='+str(collected['ingested']), flush=True)
assert collected['ingested'] > 0, 'NO_ARTICLES_COLLECTED'
now = datetime.now(timezone(timedelta(hours=9)))
day = (now.date() if (now.hour, now.minute) >= (7,30) else (now-timedelta(days=1)).date()).isoformat()
issue = job('/v1/issues/prepare', {'issue_date':day, 'retry_blocked':True})
print('LAUNCH_ISSUE_STATE='+issue['state'], flush=True)
print('LAUNCH_COUNTS='+json.dumps(issue['issue']['counts']), flush=True)
assert issue['state'] in ('READY','COMMITTED','WEB_VERIFIED'), 'ISSUE_NOT_READY'
assert issue['issue']['counts']['published_topics'] > 0, 'NO_VERIFIED_TOPICS'
published = job('/v1/issues/'+issue['issue_id']+'/publish',
    {'revision':issue['revision'], 'approval_hash':issue['approval_hash']})
print('LAUNCH_PUBLICATION_STATE='+published['state'], flush=True)
print('LAUNCH_ISSUE_ID='+published['issue_id'], flush=True)
assert published['state'] in ('COMMITTED','WEB_VERIFIED'), 'PUBLICATION_NOT_COMMITTED'
PY
