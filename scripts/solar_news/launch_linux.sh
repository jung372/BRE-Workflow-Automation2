#!/usr/bin/env bash
# Explicit operator action: connect the shared publish lock, enable solar
# collection/publishing, publish the first verified issue and activate the
# solar n8n schedules. Deliver stays inactive until Teams receipt is verified.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
release_sha=$2
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
root="$HOME/bre-solar-news"
wind_root="$HOME/bre-wind-news"
lock_root="$HOME/bre-news-locks"
container=bre-solar-news-solar-news-service-1
wind=bre-wind-news-news-service-1
n8n=n8n-personal-n8n-1
# Restarting the wind service and n8n must not overlap the morning publication.
now=$(TZ=Asia/Seoul date +%H%M)
if (( 10#$now >= 720 && 10#$now < 935 )); then echo SOLAR_LAUNCH_BLOCKED_MORNING_WINDOW; exit 1; fi
[[ -f "$root/settings/n8n-import-complete" ]] || { echo SOLAR_NOT_CONNECTED; exit 1; }
docker exec "$container" python -c '
import os, subprocess, sys
from pathlib import Path
clone = Path(os.environ["SOLAR_NEWS_PUBLISH_CLONE"])
if not (clone / ".solar-news-publish-clone").is_file(): sys.exit("SOLAR_PUBLISH_CLONE_MISSING")
probe = subprocess.run(["git","-C",str(clone),"ls-remote","--heads","origin","main"], capture_output=True, timeout=60)
if probe.returncode: sys.exit("SOLAR_PUBLISH_DEPLOY_KEY_NOT_AUTHORIZED")
print("SOLAR_PUBLISH_SSH_READY=true")'

# Both publishers push to the same branch; the wind service must hold the same lock.
if ! grep -Eq '^BRE_NEWS_LOCK_ROOT=.+$' "$wind_root/settings/compose.env"; then
    printf 'BRE_NEWS_LOCK_ROOT=%s\n' "$lock_root" >> "$wind_root/settings/compose.env"
    bash "$source_root/scripts/wind_news/install_linux.sh" "$source_root" "$release_sha"
fi
wind_lock=$(docker exec "$wind" python -c 'import os; print(os.environ.get("BRE_NEWS_PUBLISH_LOCK", ""))')
solar_lock=$(docker exec "$container" python -c 'import os; print(os.environ.get("BRE_NEWS_PUBLISH_LOCK", ""))')
[[ -n "$wind_lock" && "$wind_lock" == "$solar_lock" ]] || { echo SHARED_PUBLISH_LOCK_NOT_CONNECTED; exit 1; }
[[ "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/bre-news-locks"}}{{.Source}}{{end}}{{end}}' "$wind")" == \
   "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/bre-news-locks"}}{{.Source}}{{end}}{{end}}' "$container")" ]] \
    || { echo SHARED_PUBLISH_LOCK_MOUNT_MISMATCH; exit 1; }
echo SHARED_PUBLISH_LOCK_CONNECTED

python3 - "$root/settings/config.json" "$source_root/config" <<'PY'
import json, sys
from pathlib import Path
path, source = map(Path, sys.argv[1:])
config = json.loads(path.read_text())
if config.get('industry') != 'solar': raise SystemExit('SOLAR_CONFIG_INDUSTRY_MISMATCH')
collection = json.loads((source / 'solar_news_collection.json').read_text())
assert collection['provider'] == 'public_publishers' and all(s.get('industry') == 'solar' for s in collection['sources'])
collection['enabled'] = True
config['collection'] = collection
config['policy'].update(require_rights_review=False, require_free_access=True, auto_recover=True)
config['policy']['publisher']['enabled'] = True
# Write in place: the file is bind-mounted read-only into the running container.
path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
PY
docker restart "$container" >/dev/null
for attempt in $(seq 1 30); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
    [[ "$health" == healthy ]] && break
    sleep 2
done
[[ "$health" == healthy ]] || { echo SOLAR_HEALTH_FAILED; exit 1; }
docker exec "$container" python -c 'import json,os; from pathlib import Path; c=json.loads(Path(os.environ["SOLAR_NEWS_CONFIG"]).read_text()); assert c["collection"]["enabled"] and c["policy"]["publisher"]["enabled"], "SOLAR_CONFIG_NOT_APPLIED"; print("SOLAR_COLLECTION_AND_PUBLISHER_ENABLED")'

docker exec -i "$container" python -u - <<'PY'
from datetime import datetime, timedelta, timezone
import json, os, re, time, uuid
import requests
s = requests.Session(); s.trust_env = False
s.headers['Authorization'] = 'Bearer ' + os.environ['SOLAR_NEWS_API_TOKEN']
base = 'http://127.0.0.1:8091'
ready = s.get(base+'/health/ready', timeout=15); ready.raise_for_status()
assert ready.json().get('industry') == 'solar', 'SERVICE_INDUSTRY_MISMATCH'
def job(path, payload):
    response = s.post(base+path, json=payload, headers={'Idempotency-Key':'solar-launch-'+uuid.uuid4().hex}, timeout=20)
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
print('LAUNCH_PUBLISHERS='+json.dumps(collected.get('publisher_counts',{}),ensure_ascii=False), flush=True)
for result in collected.get('source_results',[]):
    print('LAUNCH_SOURCE='+json.dumps({k:result[k] for k in ('source_id','status','count','discovered','attempted','outcomes') if k in result},ensure_ascii=False), flush=True)
assert collected['ingested'] > 0, 'NO_ARTICLES_COLLECTED'
now = datetime.now(timezone(timedelta(hours=9)))
day = (now.date() if (now.hour, now.minute) >= (7,30) else (now-timedelta(days=1)).date()).isoformat()
issue = job('/v1/issues/prepare', {'issue_date':day, 'retry_blocked':True})
print('LAUNCH_ISSUE_STATE='+issue['state'], flush=True)
print('LAUNCH_COUNTS='+json.dumps(issue['issue']['counts']), flush=True)
assert issue['state'] in ('READY','COMMITTED','WEB_VERIFIED'), 'ISSUE_NOT_READY'
published = job('/v1/issues/'+issue['issue_id']+'/publish',
    {'revision':issue['revision'], 'approval_hash':issue['approval_hash']})
# Pages may finish after the publisher's first verification window.
for attempt in range(3):
    if published['state'] == 'WEB_VERIFIED': break
    assert published['state'] == 'COMMITTED', 'PUBLICATION_NOT_COMMITTED'
    time.sleep(20)
    published = job('/v1/issues/'+issue['issue_id']+'/publish',
        {'revision':issue['revision'], 'approval_hash':issue['approval_hash']})
print('LAUNCH_PUBLICATION_STATE='+published['state'], flush=True)
print('LAUNCH_ISSUE_ID='+published['issue_id'], flush=True)
assert published['state'] == 'WEB_VERIFIED', 'PUBLICATION_WEB_VERIFICATION_PENDING'
PY

# Activate daily schedules only after the first issue is web-verified.
python3 - "$root/settings/n8n-links.json" "$n8n" <<'PY'
import json, re, subprocess, sys, time
links, n8n = json.load(open(sys.argv[1]))['workflows'], sys.argv[2]
names = ('BRE-SOLAR-01-Collect', 'BRE-SOLAR-02-Prepare', 'BRE-SOLAR-03-Publish',
         'BRE-SOLAR-05A-Errors', 'BRE-SOLAR-05B-Reconcile', 'BRE-SOLAR-06-Backup')
def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode: raise SystemExit('SOLAR_N8N_COMMAND_FAILED')
    return result.stdout
def active():
    out = run('docker', 'exec', n8n, 'n8n', 'list:workflow', '--active=true', '--onlyId')
    return {s.strip() for s in out.splitlines() if re.fullmatch(r'[A-Za-z0-9_-]{8,64}', s.strip())}
ids = {links[name] for name in names}
before = active()
for workflow_id in sorted(ids - before):
    run('docker', 'exec', n8n, 'n8n', 'publish:workflow', '--id=' + workflow_id)
run('docker', 'restart', n8n)
for _ in range(30):
    if run('docker', 'inspect', '--format', '{{.State.Health.Status}}', n8n).strip() == 'healthy': break
    time.sleep(2)
else:
    raise SystemExit('N8N_HEALTH_FAILED')
after = active()
if after != before | ids or links['BRE-SOLAR-04-Deliver'] in after:
    raise SystemExit('SOLAR_ACTIVE_WORKFLOW_SET_MISMATCH')
print('SOLAR_N8N_SCHEDULES_ACTIVE=' + str(len(ids)) + ' DELIVER_ACTIVE=false')
print('OTHER_ACTIVE_WORKFLOWS_PRESERVED=' + str(len(before - ids)))
PY
