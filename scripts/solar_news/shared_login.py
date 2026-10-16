"""Connect existing wind OAuth to solar without reading the credential store."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

WIND = 'bre-wind-news-news-service-1'
SOLAR = 'bre-solar-news-solar-news-service-1'


def docker(*args, code=None, timeout=60):
    result = subprocess.run(['docker', *args], input=code, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        raise ValueError('SHARED_LOGIN_DOCKER_FAILED')
    return result.stdout.strip()


def auth_mount(container, industry):
    # Only mount metadata, never Config.Env or the auth file itself.
    mounts = json.loads(docker('inspect', '--format', '{{json .Mounts}}', container))
    matched = [m for m in mounts if m['Destination'] == f'/var/lib/bre-{industry}/codex-auth']
    if len(matched) != 1 or matched[0]['Type'] != 'bind' or not matched[0]['RW']:
        raise ValueError('SHARED_LOGIN_MOUNT_INVALID')
    return Path(matched[0]['Source']).resolve()


def check_login(container, industry):
    code = f'''
import os, subprocess
env = {{k: v for k, v in os.environ.items() if k in ('PATH','HOME','LANG','SSL_CERT_FILE','CODEX_CA_CERTIFICATE')}}
env['CODEX_HOME'] = os.environ['{industry.upper()}_NEWS_CODEX_HOME']
result = subprocess.run(['codex','login','status'], env=env, capture_output=True, text=True, timeout=20)
text = (result.stdout + result.stderr).lower()
print('CHATGPT_CONNECTED' if result.returncode == 0 and 'chatgpt' in text and 'api key' not in text else 'LOGIN_REQUIRED')
'''
    if docker('exec', '-i', container, 'python', '-', code=code) != 'CHATGPT_CONNECTED':
        raise ValueError('EXISTING_CHATGPT_LOGIN_REQUIRED')


def update_auth_root(settings, auth_root):
    """Change only one non-secret mount setting; preserve all private values."""
    auth_root = Path(auth_root).resolve()
    if not auth_root.is_dir() or any(c in str(auth_root) for c in '\r\n=$'):
        raise ValueError('SHARED_LOGIN_PATH_INVALID')
    settings = Path(settings)
    if settings.is_symlink():
        raise ValueError('SHARED_LOGIN_SETTINGS_INVALID')
    text = settings.read_text(encoding='utf-8')
    lines = text.splitlines()
    if any(line.startswith(('WIND_NEWS_', 'TEAMS_WEBHOOK_URL=')) for line in lines):
        raise ValueError('SHARED_LOGIN_SETTINGS_INDUSTRY_MISMATCH')
    lines = [line for line in lines if not line.startswith('SOLAR_NEWS_CODEX_AUTH_ROOT=')]
    temporary = settings.with_suffix('.shared-login.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines + ['SOLAR_NEWS_CODEX_AUTH_ROOT=' + str(auth_root)]) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, settings)


def check_shared_lock():
    holder_code = '''import os,sys
from pathlib import Path
from news.locks import file_lock
with file_lock(Path(os.environ['WIND_NEWS_CODEX_HOME'])/'news-summary.lock'):
 print('LOCK_READY', flush=True)
 sys.stdin.readline()
'''
    holder = subprocess.Popen(['docker', 'exec', '-i', WIND, 'python', '-u', '-c', holder_code],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        # Bound the readiness read even if docker/lock acquisition stalls.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(holder.stdout.readline)
            try:
                if future.result(timeout=130).strip() != 'LOCK_READY':
                    raise ValueError('SHARED_LOGIN_LOCK_HOLDER_FAILED')
            except Exception:
                holder.terminate()
                raise
        contender = '''import os
from pathlib import Path
from news.locks import file_lock
try:
 with file_lock(Path(os.environ['SOLAR_NEWS_CODEX_HOME'])/'news-summary.lock', timeout=.2):
  print('LOCK_NOT_SHARED')
except ValueError as error:
 print('LOCK_SHARED' if str(error)=='NEWS_LOCK_TIMEOUT' else 'LOCK_ERROR')
'''
        if docker('exec', '-i', SOLAR, 'python', '-', code=contender) != 'LOCK_SHARED':
            raise ValueError('SHARED_LOGIN_LOCK_NOT_SHARED')
    finally:
        if holder.poll() is None:
            holder.communicate('\n', timeout=10)
    print('CODEX_CROSS_CONTAINER_LOCK_VERIFIED')


def check_solar_model():
    # Actual two-pass inference on synthetic evidence, with a fresh private cache.
    # No collection, publication, Teams calls, or credential inspection.
    code = '''import json, os, tempfile
from pathlib import Path
from news.codex_summary import CodexSummary
from news.service import load_config
config=load_config(os.environ['SOLAR_NEWS_CONFIG'])['policy']['summarizer']
assert config['model']==config['review_model']=='gpt-6.1-sol'
root=Path(os.environ['SOLAR_NEWS_RUNTIME_DIR'])/'login-checks'
root.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=root) as temporary:
 os.environ['SOLAR_NEWS_RUNTIME_DIR']=temporary
 result=CodexSummary({**config,'industry':'solar','max_calls_per_day':2})({'title':'태양광 인증 연결 검증용 가상 문장','description':'태양광 발전 설비 점검 일정을 안내했다.','evidence_scope':'description'})
 print(json.dumps({'valid':bool(result.get('valid')),'status':result['validation_status'],'error_code':result.get('fallback_reason'),'model':config['model']}))
'''
    result = json.loads(docker('exec', '-i', SOLAR, 'python', '-', code=code, timeout=420))
    if not result.get('valid'):
        raise ValueError('SOLAR_CODEX_TWO_PASS_FAILED')
    print('SOLAR_CODEX_TWO_PASS_VERIFIED model=gpt-6.1-sol')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'configure', 'verify'))
    action = parser.parse_args().action
    root = Path.home()
    mount = auth_mount(WIND, 'wind')
    # The established wind installer preserves this standard private directory.
    if mount != (root / 'bre-wind-news' / 'codex-auth').resolve():
        raise ValueError('WIND_LOGIN_NONSTANDARD_MOUNT')
    check_login(WIND, 'wind')
    if action == 'configure':
        update_auth_root(root / 'bre-solar-news' / 'settings' / 'compose.env', mount)
        print('SOLAR_SHARED_LOGIN_CONFIGURED')
    elif action == 'verify':
        if auth_mount(SOLAR, 'solar') != mount:
            raise ValueError('SOLAR_LOGIN_MOUNT_MISMATCH')
        check_login(SOLAR, 'solar')
        check_shared_lock()
        check_solar_model()
        check_login(WIND, 'wind')
        print('WIND_AND_SOLAR_CHATGPT_LOGIN_VERIFIED')
    else:
        print('WIND_CHATGPT_LOGIN_PREFLIGHT_OK')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never expose subprocess stderr or private settings in CI logs.
        code = str(error) if isinstance(error, ValueError) and str(error).isupper() and str(error).replace('_','').isalnum() else 'SHARED_LOGIN_OPERATION_FAILED'
        print(code)
        sys.exit(1)
