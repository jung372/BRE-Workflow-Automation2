#!/usr/bin/env bash
set -euo pipefail
umask 077
root="$HOME/bre-wind-news"
container=bre-wind-news-news-service-1
n8n=n8n-personal-n8n-1
[[ -L "$root/current" ]] || { echo NEWS_NOT_INSTALLED; exit 1; }
# Publish key is dedicated to this repository, never copied from an existing account.
docker exec -i "$container" python - <<'PY'
import json, os, pathlib, subprocess, requests
runtime = pathlib.Path('/var/lib/bre-wind/runtime')
ssh = runtime / 'ssh'
ssh.mkdir(mode=0o700, exist_ok=True)
key = ssh / 'publish_ed25519'
if not key.exists():
    subprocess.run(['ssh-keygen','-t','ed25519','-N','','-C','BRE wind-news publisher','-f',str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
session = requests.Session(); session.trust_env = False
response = session.get('https://api.github.com/meta', timeout=30); response.raise_for_status()
keys = response.json()['ssh_keys']
if not keys or not all(k.startswith(('ssh-ed25519 ', 'ecdsa-sha2-nistp256 ', 'ssh-rsa ')) for k in keys): raise ValueError('INVALID_GITHUB_HOST_KEYS')
(ssh / 'known_hosts').write_text(''.join('github.com '+k+'\n' for k in keys))
subprocess.run(['git','config','--global','core.sshCommand',f'ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile={ssh / "known_hosts"}'], check=True)
clone = pathlib.Path('/var/lib/bre-wind/publish')
if not (clone / '.git').exists():
    subprocess.run(['git','clone','--depth','1','--branch','main','https://github.com/jung372/BRE-Workflow-Automation2.git',str(clone)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
    subprocess.run(['git','-C',str(clone),'remote','set-url','origin','git@github.com:jung372/BRE-Workflow-Automation2.git'],check=True)
    subprocess.run(['git','-C',str(clone),'config','user.name','BRE Wind News'],check=True)
    subprocess.run(['git','-C',str(clone),'config','user.email','wind-news@users.noreply.github.com'],check=True)
    with (clone / '.git/info/exclude').open('a') as stream: stream.write('\n.wind-news-publish-clone\n')
    (clone / '.wind-news-publish-clone').touch()
print('PUBLISH_DEPLOY_PUBLIC_KEY='+key.with_suffix('.pub').read_text().strip())
PY

if [[ -f "$root/settings/n8n-import-complete" ]]; then
    echo N8N_ALREADY_CONNECTED
    exit 0
fi
temp=$(mktemp -d "$root/settings/n8n-import.XXXXXXXX")
container_temp="/tmp/bre-wind-$(basename "$temp")"
trap 'docker exec --user root "$n8n" rm -rf "$container_temp" >/dev/null 2>&1 || true; rm -rf -- "$temp"' EXIT
python3 - "$root" "$temp" <<'PY'
import json, sys, uuid
from pathlib import Path
root, temp = map(Path, sys.argv[1:])
flows = [json.loads(p.read_text()) for p in sorted((root / 'current/automation/n8n').glob('*.json'))]
manifest = root / 'settings/n8n-links.json'
if manifest.exists(): links = json.loads(manifest.read_text())
else:
    links = {'credential_id': uuid.uuid4().hex[:16], 'workflows': {f['name']: uuid.uuid4().hex[:16] for f in flows}}
    manifest.write_text(json.dumps(links, indent=2))
credential = {'id':links['credential_id'],'name':'BRE Wind Service','type':'httpHeaderAuth',
              'data':{'name':'Authorization','value':'Bearer '+(root/'settings/service-token').read_text().strip()}}
(temp / 'credential.json').write_text(json.dumps([credential]))
for flow in flows:
    flow['id'] = links['workflows'][flow['name']]
    if flow['name'] != 'BRE-WIND-05A-Errors': flow['settings']['errorWorkflow'] = links['workflows']['BRE-WIND-05A-Errors']
    for node in flow['nodes']:
        if 'httpHeaderAuth' in node.get('credentials', {}): node['credentials']['httpHeaderAuth']['id'] = links['credential_id']
(temp / 'workflows.json').write_text(json.dumps(flows))
PY
docker exec "$n8n" mkdir -m 700 "$container_temp"
docker cp "$temp/credential.json" "$n8n:$container_temp/credential.json" >/dev/null
docker cp "$temp/workflows.json" "$n8n:$container_temp/workflows.json" >/dev/null
docker exec --user root "$n8n" chown -R node:node "$container_temp"
if ! docker exec "$n8n" n8n import:credentials --input="$container_temp/credential.json" >"$temp/credential.log" 2>&1; then
    echo N8N_CREDENTIAL_IMPORT_FAILED; exit 1
fi
if ! docker exec "$n8n" n8n import:workflow --input="$container_temp/workflows.json" >"$temp/workflows.log" 2>&1; then
    echo N8N_WORKFLOW_IMPORT_FAILED; exit 1
fi
touch "$root/settings/n8n-import-complete"
echo N8N_IMPORTED_7_INACTIVE_WORKFLOWS
python3 - "$root/settings/n8n-links.json" <<'PY'
import json, sys
print(json.dumps(json.load(open(sys.argv[1]))['workflows']))
PY
