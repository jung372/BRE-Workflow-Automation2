"""Actions client for the authenticated local news service; never print secrets."""
from datetime import date, datetime, timedelta, timezone
import json
import os
import re

import requests

KST = timezone(timedelta(hours=9))


def report(session, mode, issue_date="", *, clock=None):
    if mode not in {"preview", "send", "scheduled"}:
        raise ValueError("INVALID_REPORT_MODE")
    today = (clock or (lambda: datetime.now(KST)))().astimezone(KST).date().isoformat()
    day = issue_date or today
    if date.fromisoformat(day).isoformat() != day or mode == "scheduled" and day != today:
        raise ValueError("INVALID_REPORT_DATE")
    base = "http://127.0.0.1:8090"
    response = session.get(base + "/v1/deliveries/status", params={"issue_date": day}, timeout=60)
    response.raise_for_status()
    service_status = response.json()
    configured = bool(service_status["teams_configured"])
    payload = {"issue_date": day}
    if mode == "preview":
        response = session.post(base + "/v1/deliveries/preview", json=payload, timeout=60)
        if response.status_code == 409:
            return {"status": "SKIPPED", "issue_date": day, "teams_configured": configured,
                    "error_code": "WEB_VERIFICATION_REQUIRED"}, 0
        response.raise_for_status()
        payload_card = response.json()
        if payload_card.get("@type") == "MessageCard":
            count = sum("](" in item.get("value", "") for item in payload_card["sections"][0]["facts"])
            card_format = "messagecard"
        else:
            card = payload_card["attachments"][0]["content"]
            count = sum("](" in item.get("text", "") for item in card["body"])
            card_format = "adaptive"
        prior = service_status.get("delivery_status")
        if prior not in {None, "CLAIMED", "ACCEPTED", "DELIVERED", "FAILED", "UNKNOWN"}:
            raise ValueError("INVALID_DELIVERY_STATUS")
        return {"status": "PREVIEWED", "issue_date": day, "teams_configured": configured,
                "article_count": count, "card_format": card_format, "delivery_status": prior,
                "error_code": ""}, 0
    payload.update(channel_id="wind-news", message_type="daily", scheduled=mode == "scheduled")
    response = session.post(base + "/v1/deliveries/send", json=payload, timeout=90)
    response.raise_for_status()
    value = response.json()
    state = value.get("status")
    if state not in {"ACCEPTED", "DELIVERED", "CLAIMED", "FAILED", "UNKNOWN", "SKIPPED"}:
        raise ValueError("INVALID_DELIVERY_STATUS")
    code = value.get("error_code", "")
    if not isinstance(code, str) or code and not re.fullmatch(r"[A-Za-z0-9_]{1,100}", code):
        code = "DELIVERY_ERROR"
    result = {"status": state, "issue_date": day, "teams_configured": configured, "error_code": code}
    if isinstance(value.get("http_status"), int) and 100 <= value["http_status"] <= 599:
        result["http_status"] = value["http_status"]
    for field in ("response_class", "receiver_error_code"):
        if isinstance(value.get(field), str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", value[field]):
            result[field] = value[field]
    if state == "UNKNOWN":
        result["operator_action"] = "CHECK_TEAMS_WORKFLOW_HISTORY_NO_RESEND"
    return result, int(state in {"FAILED", "UNKNOWN"} or code in {
        "PUBLICATION_NOT_READY_BY_DEADLINE", "BRIEFING_DELIVERY_DEADLINE_MISSED", "TEAMS_NOT_CONFIGURED"})


def main():
    mode = os.environ.get("NEWS_REPORT_MODE", "preview")
    day = os.environ.get("NEWS_REPORT_DATE", "")
    try:
        with requests.Session() as session:
            session.trust_env = False
            session.headers["Authorization"] = "Bearer " + os.environ["WIND_NEWS_API_TOKEN"]
            result, code = report(session, mode, day)
    except Exception:
        result, code = {"status": "FAILED", "issue_date": datetime.now(KST).date().isoformat(),
                        "teams_configured": False, "error_code": "NEWS_REPORT_CLIENT_FAILED"}, 1
    print("NEWS_BRIEFING_RESULT=" + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if result.get("error_code") == "TEAMS_NOT_CONFIGURED":
        print("::warning::뉴스용 Teams 수신 주소가 미설정입니다. 발송되지 않았습니다.")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
