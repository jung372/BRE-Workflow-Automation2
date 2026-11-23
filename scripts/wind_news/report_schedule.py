"""Report the linked n8n schedule without exposing IDs or credentials."""
import json
from pathlib import Path
import subprocess
import sys


def main():
    industry, root = sys.argv[1:]
    assert industry in {'wind', 'solar'}
    manifest = Path(root) / 'settings' / 'n8n-links.json'
    if not manifest.is_file():
        status = 'unlinked'
    else:
        name = f'BRE-{industry.upper()}-04-Deliver'
        linked = json.loads(manifest.read_text(encoding='utf-8'))['workflows'][name]
        active = subprocess.run(['docker', 'exec', 'n8n-personal-n8n-1', 'n8n', 'list:workflow', '--active=true', '--onlyId'],
                                check=True, capture_output=True, text=True, timeout=60)
        status = 'active' if linked in active.stdout.split() else 'inactive'
    print(f'MORNING_DELIVER_WORKFLOW industry={industry} status={status}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError):
        print('MORNING_DELIVER_WORKFLOW_STATUS_UNAVAILABLE')
