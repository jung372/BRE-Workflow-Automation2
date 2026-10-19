from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def prev_weekday(date):
    """월요일이면 3일 전(금요일), 그 외엔 1일 전을 반환."""
    return date - timedelta(days=3 if date.weekday() == 0 else 1)


def get_new_items(state: dict, site_display: dict, now=None) -> tuple:
    """
    금일 08:00 스냅샷(B) - 전일 08:00 스냅샷(A) 로 신규 아이템 추출.
    반환: (new_items_list, today_key, prev_key)
    """
    now       = now or datetime.now(KST)
    today_key = now.date().strftime("%Y-%m-%d") + " 08"
    prev_key  = prev_weekday(now.date()).strftime("%Y-%m-%d") + " 08"

    all_new = []
    for site_id, site_state in state.items():
        if not isinstance(site_state, dict):
            continue
        hourly  = site_state.get("hourly_snapshots", {})
        b_items = hourly.get(today_key, [])
        a_ids   = {item["id"] for item in hourly.get(prev_key, [])}
        for item in b_items:
            if item["id"] not in a_ids:
                all_new.append({
                    "site_name": site_display.get(site_id, site_id),
                    "title":     item.get("title", ""),
                    "date":      item.get("date", ""),
                    "url":       item.get("url", "#"),
                    # 정보공개 청구 항목만 값을 가진다. 나머지는 빈 문자열.
                    "dept":      item.get("dept", ""),
                    "keyword":   item.get("keyword", ""),
                })

    return all_new, today_key, prev_key
