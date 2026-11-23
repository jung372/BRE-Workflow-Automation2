"""Time windows and persistent identities for the daily news jobs."""
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))
MORNING_START = time(7, 30)
FIRST_SEND = time(8, 30)
SEND_DEADLINE = time(9, 30)
MAINTENANCE_PAUSE = time(6, 30)
PURGE_LAST_START = time(6, 15)

WORKFLOWS = {
    'collect': ('01-Collect',), 'prepare': ('02-Prepare',), 'publish': ('03-Publish',),
    'reconcile': ('05A-Errors', '05B-Reconcile'), 'backup': ('06-Backup',),
}


def slot(now):
    return now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0).strftime('%Y-%m-%d-%H-%M')


def scheduled_job(operation, key, payload, now):
    """Recognize n8n and existing recovery jobs without changing manual keys."""
    now = now.astimezone(KST)
    day = now.date().isoformat()
    parts = key.split(':')
    if len(parts) == 3 and parts[0] == 'news-schedule':
        group = parts[1]
        if group != operation and (operation, group) != ('collect', 'cutoff'):
            return None
    elif operation == 'briefing' and key.startswith('morning-briefing-'):
        group = 'briefing'
    elif operation in {'prepare', 'publish'} and key.startswith('recovery-' + operation + '-'):
        group = operation
    elif any(parts[0] == f'BRE-{profile}-{name}' for name in WORKFLOWS.get(operation, ()) for profile in ('WIND', 'SOLAR')):
        group = 'cutoff' if operation == 'collect' and MORNING_START <= now.time() < time(7, 40) else operation
    else:
        return None
    try:
        if operation in {'prepare', 'briefing'}:
            day = payload['issue_date']
        elif operation == 'publish':
            day = payload['issue_id'].partition('-')[2]
        midnight = datetime.combine(date.fromisoformat(day), time(), KST)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    end = midnight + timedelta(hours=9, minutes=30) if group in {'briefing', 'cutoff'} else midnight + timedelta(days=1)
    return group, day, end.astimezone(timezone.utc).isoformat()
