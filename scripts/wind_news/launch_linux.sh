#!/usr/bin/env bash
# Explicit operator action: enable public-source collection and publish first issue.
set -euo pipefail
umask 077
source_root=$(realpath "$1")
root="$HOME/bre-wind-news"
container=bre-wind-news-news-service-1
mode=${3:-launch}
[[ "$mode" == launch || "$mode" == correct ]] || exit 2
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
docker exec -i -e WIND_NEWS_LAUNCH_MODE="$mode" "$container" python -u - <<'PY'
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
if os.environ.get('WIND_NEWS_LAUNCH_MODE') == 'correct':
    # Correct already-approved but unpublished metadata through the audited API.
    # Previously held/unapproved candidates are never promoted by this repair.
    from news.pipeline import classify
    response = s.get(base+'/v1/issues', timeout=15); response.raise_for_status()
    repaired = 0
    for pending in response.json()['issues']:
        if pending['state'] != 'READY': continue
        for candidate in pending['candidates']:
            if not candidate['approved'] or candidate['held']: continue
            item = candidate['item']
            response = s.get(base+'/v1/articles/'+item['representative_article_id'], timeout=15)
            response.raise_for_status(); article = response.json()
            facts = classify({**article['article'], **article['evidence']})
            changes = {k: facts[k] for k in ('contract_stage','primary_category') if item[k] != facts[k]}
            if not changes: continue
            if 'contract_stage' in changes:
                changes['tags'] = [t for t in item.get('tags', []) if t != item['contract_stage']]
                if facts['contract_stage'] != '미확인': changes['tags'].append(facts['contract_stage'])
            path = base+'/v1/issues/'+pending['issue_id']+'/review'
            for action in ('edit','approve'):
                payload = {'action':action, 'revision':pending['revision'], 'approval_hash':pending['content_hash'],
                    'item_ids':[item['event_id']], 'actor':'classification-maintenance',
                    'reason':'분류 규칙의 문자열 경계 정정; 기존 OAuth 검증 요약 유지'}
                if action == 'edit': payload['changes'] = changes
                response = s.post(path, json=payload, timeout=20); response.raise_for_status()
                pending = response.json()
            repaired += 1
    print('LAUNCH_PENDING_METADATA_REPAIRED='+str(repaired), flush=True)
collected = job('/v1/collect', {})
print('LAUNCH_INGESTED='+str(collected['ingested']), flush=True)
print('LAUNCH_PUBLISHERS='+json.dumps(collected.get('publisher_counts',{}),ensure_ascii=False), flush=True)
for result in collected.get('source_results',[]):
    print('LAUNCH_SOURCE='+json.dumps({k:result[k] for k in ('source_id','status','count','discovered','attempted','outcomes') if k in result},ensure_ascii=False), flush=True)
assert collected['ingested'] > 0, 'NO_ARTICLES_COLLECTED'
now = datetime.now(timezone(timedelta(hours=9)))
day = (now.date() if (now.hour, now.minute) >= (7,30) else (now-timedelta(days=1)).date()).isoformat()
prepare = {'issue_date':day, 'retry_blocked':True}
if os.environ.get('WIND_NEWS_LAUNCH_MODE') == 'correct':
    response = s.get(base+'/v1/issues', timeout=15); response.raise_for_status()
    completed = [i for i in response.json()['issues'] if i['state'] in ('COMMITTED','WEB_VERIFIED')]
    assert completed, 'NO_PUBLISHED_ISSUE_TO_CORRECT'
    prepare['issue_date'] = max(i['issue']['issue_date'] for i in completed)
    prepare['correction_reason'] = '풍력산업 전반의 검색어·기업·프로젝트 검색으로 매체 범위를 확대하고 원문 근거로 재발간'
issue = job('/v1/issues/prepare', prepare)
print('LAUNCH_ISSUE_STATE='+issue['state'], flush=True)
print('LAUNCH_COUNTS='+json.dumps(issue['issue']['counts']), flush=True)
assert issue['state'] in ('READY','COMMITTED','WEB_VERIFIED'), 'ISSUE_NOT_READY'
assert issue['issue']['counts']['published_topics'] > 0, 'NO_VERIFIED_TOPICS'
published = job('/v1/issues/'+issue['issue_id']+'/publish',
    {'revision':issue['revision'], 'approval_hash':issue['approval_hash']})
# Pages may finish after the publisher's first verification window. Retry the
# same immutable revision, including when this run crosses Korean midnight.
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
