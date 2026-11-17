"""Read-only live verification of the portal maintenance handling."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_config import ensure_utf8_stdout
ensure_utf8_stdout()
from config import SITES
from scrapers.open_portal import MAINTENANCE_MESSAGE, fetch_open_portal
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    items, error = fetch_open_portal(next(site for site in SITES if site['id'] == 'open_portal'), playwright)
    print('maintenance_detected', error == MAINTENANCE_MESSAGE, flush=True)
    print('no_false_empty_result', items is None, flush=True)
    if error != MAINTENANCE_MESSAGE or items is not None:
        raise SystemExit('Unexpected portal response; inspect before publishing')
