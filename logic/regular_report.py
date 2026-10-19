"""Server-clock Teams report with persistent daily claims and bounded retries.

This operational ledger is separate from scraper state and news delivery data.
Only definite rejections are retried; an interrupted POST remains UNKNOWN.
"""
from contextlib import contextmanager
from datetime import datetime, time
import json
import os
from pathlib import Path

import requests

from logic.detector import KST, get_new_items
from presentation.teams_card import SITE_DISPLAY, MUTED_METMASTS, build_card

TERMINAL = {"ACCEPTED", "UNKNOWN"}


@contextmanager
def report_lock(path):
    """OS-owned lock: process termination releases it without stale-lock races."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        locked = False
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        try:
            yield locked
        finally:
            if locked:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_json(path):
    # Invalid/missing input must not be mistaken for an empty successful scrape.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def prepare_report(state, status, now):
    if not isinstance(state, dict) or not isinstance(status, dict):
        raise ValueError("INVALID_REPORT_DATA")
    _, today, previous = get_new_items(state, SITE_DISPLAY, now=now)
    missing = []
    missing_previous = []
    for site in SITE_DISPLAY:
        snapshots = state.get(site, {}).get("hourly_snapshots", {})
        if today not in snapshots:
            missing.append(site)
        if previous not in snapshots:
            missing_previous.append(site)
    # The old detector treats absent baselines as all-new. Exclude those sites
    # rather than inventing new notices, but preserve other healthy sources.
    valid = {key: value for key, value in state.items()
             if key in SITE_DISPLAY and key not in missing_previous}
    items, _, _ = get_new_items(valid, SITE_DISPLAY, now=now)
    checked = datetime.strptime(status.get("checked_at", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    fresh = checked.date() == now.date() and checked.hour >= 8 and checked <= now
    metmasts = status.get("metmasts", []) if fresh else []
    offline = [m for m in metmasts if m.get("status") != "Online" and m.get("id") not in MUTED_METMASTS]
    return {
        "items": items, "metmasts": metmasts, "offline_count": len(offline),
        "new_count": len(items), "missing_sites": missing,
        "missing_baselines": missing_previous, "status_fresh": fresh,
        "comparison": [previous, today],
    }


def post_report(webhook, card):
    try:
        response = requests.post(webhook, json=card, timeout=(5, 15), allow_redirects=False)
        code = response.status_code
        if 200 <= code < 300:
            return {"status": "ACCEPTED", "http_status": code}
        if 300 <= code < 500:
            return {"status": "FAILED", "http_status": code}
        return {"status": "UNKNOWN", "http_status": code}
    except requests.ConnectTimeout:
        return {"status": "FAILED", "reason": "CONNECT_TIMEOUT"}
    except requests.RequestException:
        # Never expose signed URL, response body, or request exception text.
        return {"status": "UNKNOWN", "reason": "TRANSPORT_AMBIGUOUS"}


def run_report(data_dir, runtime_dir, webhook="", *, clock=None, sender=None,
               preview=False, manual=False, force=False):
    clock = clock or (lambda: datetime.now(KST))
    sender = sender or post_report
    now = clock().astimezone(KST)
    data_dir, runtime_dir = Path(data_dir), Path(runtime_dir)
    ledger_path = runtime_dir / "teams-report" / (now.date().isoformat() + ".json")
    with report_lock(runtime_dir / "teams-report" / "report.lock") as locked:
        if not locked:
            return {"status": "BUSY"}
        try:
            ledger = read_json(ledger_path) if ledger_path.exists() else {"attempts": []}
            if not isinstance(ledger, dict) or not isinstance(ledger.get("attempts"), list):
                raise ValueError("INVALID_LEDGER")
        except (OSError, ValueError):
            return {"status": "LEDGER_INVALID"}
        if not preview and ledger.get("status") in TERMINAL:
            return {"status": ledger["status"], "reason": "DAILY_CLAIM_EXISTS"}
        if not manual and not preview:
            if now.weekday() >= 5 or now.time() < time(8, 5):
                return {"status": "OUTSIDE_WINDOW"}
            if now.time() >= time(9):
                outcome = {"status": "NO_NEW_ITEMS" if ledger.get("status") == "NO_NEW_ITEMS" else "DEADLINE_MISSED"}
                ledger.update(outcome, checked_at=now.isoformat())
                save_json(ledger_path, ledger)
                return outcome
        if (runtime_dir / "run.lock").exists() or (runtime_dir / "deploy.lock").exists():
            if not preview:
                ledger.update(status="DATA_BUSY", checked_at=now.isoformat())
                save_json(ledger_path, ledger)
            return {"status": "DATA_BUSY"}
        try:
            prepared = prepare_report(read_json(data_dir / "last_state.json"),
                                      read_json(data_dir / "data/status.json"), now)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            if not preview:
                ledger.update(status="DATA_NOT_READY", checked_at=now.isoformat())
                save_json(ledger_path, ledger)
            return {"status": "DATA_NOT_READY", "reason": "INVALID_OR_MISSING_SNAPSHOT"}
        summary = {key: value for key, value in prepared.items() if key not in {"items", "metmasts"}}
        if preview:
            return {"status": "PREVIEWED", "delivery_status": ledger.get("status", "NOT_ATTEMPTED"), **summary}
        complete = not prepared["missing_sites"] and not prepared["missing_baselines"] and prepared["status_fresh"]
        has_news = bool(prepared["items"] or prepared["offline_count"])
        if not prepared["status_fresh"] or (not complete and not has_news):
            outcome = {"status": "DATA_NOT_READY", **summary}
        elif not manual and now.time() < time(8, 30):
            outcome = {"status": "READY", **summary}
        elif not has_news and not force:
            outcome = {"status": "NO_NEW_ITEMS", **summary}
        else:
            outcome = None
        if outcome:
            ledger.update(outcome, checked_at=now.isoformat())
            save_json(ledger_path, ledger)
            return outcome
        if not webhook:
            return {"status": "NOT_CONFIGURED"}
        slot = now.strftime("%Y-%m-%d %H:") + f"{now.minute // 5 * 5:02d}"
        if any(a["slot"] == slot for a in ledger["attempts"]):
            return {"status": "SLOT_ALREADY_ATTEMPTED"}
        sending_at = clock().astimezone(KST)
        if sending_at.date() != now.date() or (not manual and sending_at.time() >= time(8, 59, 30)):
            return {"status": "DEADLINE_MISSED"}
        attempt = {"slot": slot, "started_at": sending_at.isoformat(), "status": "UNKNOWN"}
        ledger["attempts"].append(attempt)
        ledger.update(summary, status="UNKNOWN", checked_at=sending_at.isoformat())
        save_json(ledger_path, ledger)
        card = build_card(prepared["items"], prepared["metmasts"])
        incomplete = sorted(set(prepared["missing_sites"] + prepared["missing_baselines"]))
        if incomplete:
            card["sections"].append({"text": "비교 데이터 미확보: " + ", ".join(SITE_DISPLAY[s] for s in incomplete)})
        result = sender(webhook, card)
        attempt.update(result)
        ledger.update(result)
        save_json(ledger_path, ledger)
        return {**result, **summary}
