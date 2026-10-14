"""Teams dispatch boundary. Secrets and response bodies never enter the ledger.

The service claims an issue/revision before calling this function. Any ambiguous
network result is UNKNOWN and must be reconciled rather than blindly retried.
"""

from __future__ import annotations

from datetime import date
import re
from urllib.parse import quote, urlsplit

import requests

from .collector import safe_public_url


def _public_https(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443))
    except (ValueError, TypeError):
        return False


def _webhook_allowed(url: str) -> bool:
    if not _public_https(url):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    return (any(host.endswith("." + suffix) for suffix in
                ("logic.azure.com", "api.powerplatform.com", "webhook.office.com"))
            or host == "outlook.office.com" and parsed.path.startswith("/webhook/"))


def _plain(value: object, limit: int) -> str:
    # Adaptive Card TextBlock supports Markdown. Escape article-controlled syntax.
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]
    return re.sub(r"([\\`*_{}\[\]()#+!<>])", r"\\\1", text)


def build_card(issue: dict, site_url: str, card_format: str = "adaptive") -> dict:
    if card_format not in {"adaptive", "messagecard"}:
        raise ValueError("invalid_card_format")
    day = issue.get("issue_date", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("invalid_issue_date")
    date.fromisoformat(day)
    if not _public_https(site_url):
        raise ValueError("invalid_site_url")
    report_url = site_url.rstrip("/") + "/#/daily/" + day
    items = issue.get("items", [])
    held = int(issue.get("counts", {}).get("held", 0))
    overview = f"주요 사건 {len(items)}건 · 집계 마감 07:30 KST"
    if held:
        overview += f" · 검토 중 {held}건"
    body = [
        {"type": "TextBlock", "text": f"[풍력 일간 시황] {day}",
         "weight": "Bolder", "size": "Large", "wrap": True},
        {"type": "TextBlock", "text": overview,
         "wrap": True, "isSubtle": True},
    ]
    if issue.get("revision", 1) > 1:
        body.append({"type": "TextBlock", "text": "정정된 보고서입니다.", "wrap": True})
    if not items:
        body.append({"type": "TextBlock", "text": "오늘 새로 선정된 주요 기사가 없습니다.", "wrap": True})
    for rank, item in enumerate(items, 1):
        url = item.get("source_url", "")
        if not _public_https(url) or re.search(r"[\s\x00-\x1f\x7f]", url):
            raise ValueError("invalid_article_url")
        url = safe_public_url(url, [urlsplit(url).hostname])
        # Encode Markdown delimiters in URLs; preserve query strings and escapes.
        url = quote(url, safe=":/?&=#%+;,@!$~*-._")
        title = _plain(item.get("headline"), 180)
        source = _plain(item.get("source_name"), 100)
        if not title or not source:
            raise ValueError("article_title_and_source_required")
        body.append({"type": "TextBlock", "text": f"{rank}. [{title}]({url}) — {source}",
                     "wrap": True})
    if card_format == "messagecard":
        # The existing report.yml receiver consumes MessageCard sections/facts.
        return {"@type": "MessageCard", "@context": "https://schema.org/extensions",
                "themeColor": "8FC31F", "summary": body[0]["text"], "title": body[0]["text"],
                "sections": [{"facts": [{"name": "", "value": block["text"]} for block in body[1:]]},
                             {"facts": [{"name": "", "value": f"[웹에서 전체 보고서 보기]({report_url})"}]}],
                "potentialAction": [{"@type": "OpenUri", "name": "웹에서 전체 보고서 보기",
                                     "targets": [{"os": "default", "uri": report_url}]}]}
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard", "version": "1.4", "body": body,
            "actions": [{"type": "Action.OpenUrl", "title": "웹에서 전체 보고서 보기", "url": report_url}]}
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


def send_card(issue: dict, webhook_url: str, site_url: str, session=None, *, card_format="adaptive") -> dict:
    """Return an outcome safe for logs. Do not infer channel delivery from 2xx."""
    if not webhook_url:
        return {"status": "SKIPPED", "error_code": "teams_not_configured"}
    if not _webhook_allowed(webhook_url):
        return {"status": "FAILED", "error_code": "teams_url_not_allowed"}
    try:
        payload = build_card(issue, site_url, card_format)
    except (ValueError, TypeError, KeyError):
        return {"status": "FAILED", "error_code": "invalid_card_input"}
    client = session or requests.Session()
    try:
        response = client.post(webhook_url, json=payload, timeout=(5, 25), allow_redirects=False)
        code = response.status_code
        if 200 <= code < 300:
            return {"status": "ACCEPTED", "http_status": code}
        if 400 <= code < 500 or 300 <= code < 400:
            return {"status": "FAILED", "error_code": "teams_rejected", "http_status": code}
        outcome = {"status": "UNKNOWN", "error_code": "teams_ambiguous_response", "http_status": code,
                   "response_class": "receiver_server_error" if 500 <= code < 600 else "unexpected_http_status"}
        # Only known, fixed receiver codes leave this boundary. Never log body,
        # headers, request IDs, signed URLs or arbitrary upstream messages.
        try:
            receiver_code = response.json().get("error", {}).get("code")
            if receiver_code in {"WorkflowTriggerIsNotEnabled", "WorkflowNotFound", "WorkflowRunActionFailure",
                                 "TriggerRequestMethodNotValid", "InternalServerError", "BadGateway", "ServiceUnavailable"}:
                outcome["receiver_error_code"] = receiver_code
        except (ValueError, AttributeError, TypeError):
            pass
        return outcome
    except requests.RequestException:
        # Even a connection reset may happen after the receiver accepted the card.
        return {"status": "UNKNOWN", "error_code": "teams_transport_ambiguous"}
    finally:
        if session is None:
            client.close()
