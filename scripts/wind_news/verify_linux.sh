#!/usr/bin/env bash
# Verify the deployed service; outputs contain only counts, states and hashes.
set -euo pipefail
umask 077
root="$HOME/bre-wind-news"
source_root=$(realpath "$1")
mkdir -p "$root/operator"
cp "$source_root/scripts/wind_news/setup_naver.py" "$root/operator/"
container=bre-wind-news-news-service-1
docker exec -i "$container" python - <<'PY'
import hashlib, json, os, pathlib, subprocess, time, uuid
import duckdb, requests
s = requests.Session(); s.trust_env = False
base = 'http://127.0.0.1:8090'
assert s.get(base+'/health/ready', timeout=5).status_code == 401
s.headers['Authorization'] = 'Bearer '+os.environ['WIND_NEWS_API_TOKEN']
health = s.get(base+'/health/ready', timeout=5); health.raise_for_status()
print('NEWS_AUTHENTICATED_HEALTH='+health.json()['status'])
response = s.post(base+'/v1/backup',json={},headers={'Idempotency-Key':'server-smoke-'+uuid.uuid4().hex},timeout=10)
response.raise_for_status(); job_id=response.json()['job_id']
for _ in range(30):
    # Reads share the single writer queue; a backup can hold it beyond 5 seconds.
    response=s.get(base+'/v1/jobs/'+job_id,timeout=60); response.raise_for_status(); job=response.json()
    if job['status'] not in ('QUEUED','RUNNING'): break
    time.sleep(1)
assert job['status']=='SUCCEEDED', 'BACKUP_JOB_FAILED'
backup=pathlib.Path(os.environ['WIND_NEWS_RUNTIME_DIR'])/'backups'/job['result']['filename']
assert hashlib.sha256(backup.read_bytes()).hexdigest()==job['result']['sha256']
with duckdb.connect(str(backup),read_only=True) as db:
    counts={table:db.execute('select count(*) from '+table).fetchone()[0] for table in ('articles','issues','jobs')}
    last_batch=db.execute('SELECT created_at,source_results FROM batches ORDER BY created_at DESC LIMIT 1').fetchone()
    if last_batch:
        print('LAST_COLLECTION_AT='+last_batch[0])
        print('LAST_COLLECTION_RESULTS='+last_batch[1])
print('BACKUP_HASH_VERIFIED='+json.dumps(counts))
env=dict(os.environ,CODEX_HOME=os.environ['WIND_NEWS_CODEX_HOME'])
status=subprocess.run(['codex','login','status'],env=env,capture_output=True,text=True,timeout=15)
text=(status.stdout+status.stderr).lower()
oauth=status.returncode==0 and 'chatgpt' in text and 'api key' not in text
print('CODEX_OAUTH_CONNECTED='+str(oauth).lower())
if oauth:
    from news.codex_summary import CodexSummary
    from news.service import load_config
    def checked_runner(args, **kwargs):
        stage = 'EXEC' if len(args)>1 and args[1]=='exec' else 'LOGIN'
        try:
            response = subprocess.run(args, **kwargs)
        except Exception as error:
            safe_type = type(error).__name__
            print('CODEX_PROCESS_ERROR='+ (safe_type if safe_type in ('PermissionError','FileNotFoundError','TimeoutExpired') else 'OTHER'))
            raise
        print('CODEX_PROCESS_'+stage+'_EXIT='+str(response.returncode))
        if response.returncode:
            message=(response.stderr or '').lower()
            # Fixed categories only. Never expose CLI stderr, paths, tokens or prompts.
            checks = {
                'CLI_ARGUMENT': 'unexpected argument' in message or 'unrecognized option' in message,
                'SCHEMA': 'schema' in message and ('invalid' in message or 'unsupported' in message),
                'MODEL': 'model' in message and ('not supported' in message or 'not found' in message or 'does not exist' in message),
                'RATE_LIMIT': 'rate limit' in message or 'usage limit' in message,
                'READ_ONLY_FS': 'read-only file system' in message,
                'PERMISSION': 'permission denied' in message or 'operation not permitted' in message,
                'NETWORK': 'connection' in message or 'certificate' in message or 'timed out' in message,
                'AUTH': 'unauthorized' in message or '401' in message,
                'CONFIG': 'config' in message and ('invalid' in message or 'error' in message),
            }
            for code, matched in checks.items():
                if matched: print('CODEX_PROCESS_DIAGNOSTIC='+code)
            if not any(checks.values()): print('CODEX_PROCESS_DIAGNOSTIC=UNCLASSIFIED')
        return response
    result=CodexSummary(load_config(os.environ['WIND_NEWS_CONFIG'])['policy']['summarizer'],runner=checked_runner)({
        'title':'풍력 발전 점검 안내', 'description':'풍력 발전 설비 점검 일정을 안내했다.', 'evidence_scope':'description'})
    print('CODEX_TWO_PASS_RESULT='+result['validation_status'])
    if not result.get('valid'): print('CODEX_TEST_CODE='+result.get('fallback_reason','UNKNOWN'))
    assert result.get('valid'), 'CODEX_TWO_PASS_NOT_VERIFIED'
from news.service import load_config
config=load_config(os.environ['WIND_NEWS_CONFIG'])
from datetime import datetime,timezone
print('SERVER_UTC='+datetime.now(timezone.utc).isoformat())
print('SEARCH_QUERY_COUNT='+str(len(config['collection'].get('queries',[]))))
print('FREE_ONLY_POLICY='+str(config['policy'].get('require_free_access',False)).lower())
print('COLLECTION_ENABLED='+str(config['collection'].get('enabled',False)).lower())
git=subprocess.run(['git','-C','/var/lib/bre-wind/publish','ls-remote','--exit-code','origin','refs/heads/main'],capture_output=True,text=True,timeout=30)
print('PUBLISH_REPO_ACCESS='+str(git.returncode==0).lower())
PY
docker exec n8n-personal-n8n-1 node -e "fetch('http://news-service:8090/health/ready').then(r=>{console.log('N8N_TO_NEWS_HTTP='+r.status);if(r.status!==401)process.exitCode=1}).catch(()=>{console.log('N8N_TO_NEWS_FAILED');process.exitCode=1})"
docker inspect "$container" --format 'RESTART_POLICY={{.HostConfig.RestartPolicy.Name}} HEALTH={{.State.Health.Status}}'
