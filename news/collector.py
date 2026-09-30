"""Legacy Naver response parser; live Naver discovery is blocked.

The transport contract is ``get(url, headers=..., timeout=..., allow_redirects=False)``
and returns a requests-compatible response for historical offline fixtures only.
Current Naver search terms prohibit AI input and permanent archive storage.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

NAVER_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"


def clean_text(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()


def safe_public_url(url, allowed_hosts, *, resolver=None, resolve=False):
    """Validate before a request. Redirects must be separately validated.

    A literal host match is required: an allowlist entry never permits subdomains.
    DNS validation is enabled at network boundaries, not during metadata parsing.
    """
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("unsafe_url")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("unsafe_url")
    if host not in {str(h).lower().rstrip(".") for h in allowed_hosts} or "\\" in url:
        raise ValueError("unapproved_host")
    if host in ("localhost", "metadata.google.internal") or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("private_host")
    if resolve:
        addresses = (resolver or socket.getaddrinfo)(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise ValueError("private_address")
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def _instant(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


class Collector:
    def __init__(self, config, transport=None, clock=None):
        self.config = config.get("collection", config)
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, since=None, until=None):
        cfg = self.config
        sources = [source for source in cfg.get("sources", []) if source.get("enabled", True)]
        def result(status, code=None, count=0):
            return {"articles": [], "source_results": [dict(source_id=s["source_id"], status=status,
                    count=count, **({"error_code": code} if code else {})) for s in sources]}
        if not cfg.get("enabled", False):
            return result("disabled", "COLLECTION_DISABLED")
        if cfg.get("provider", "naver") != "naver":
            return result("request_error", "UNSUPPORTED_PROVIDER")
        if self.transport is None:
            return result("disabled", "NAVER_TERMS_INCOMPATIBLE")
        queries = cfg.get("queries", cfg.get("keywords", []))
        if not sources or not queries:
            return result("parse_error", "COLLECTION_CONFIG_INVALID")
        end = _instant(until or self.clock())
        start = _instant(since) if since else end - timedelta(hours=max(72, cfg.get("overlap_hours", 72)))
        start = min(start, end - timedelta(hours=72))
        transport = self.transport
        headers = {}
        if transport is None:
            client_id = os.environ.get(cfg.get("client_id_env", "WIND_NEWS_NAVER_CLIENT_ID"))
            secret = os.environ.get(cfg.get("client_secret_env", "WIND_NEWS_NAVER_CLIENT_SECRET"))
            if not client_id or not secret:
                return result("auth_error", "NAVER_CREDENTIALS_MISSING")
            import requests
            transport = requests.Session()
            transport.trust_env = False
            headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": secret}
        page_size = min(100, max(1, int(cfg.get("page_size", 100))))
        max_pages = min(1000 // page_size, max(1, int(cfg.get("max_pages", 10))))
        found, errors, saturated, invalid = {}, [], False, 0
        for query in queries:
            for page in range(max_pages):
                try:
                    response = transport.get(NAVER_ENDPOINT, params={"query": query, "sort": "date",
                        "display": page_size, "start": page * page_size + 1}, headers=headers,
                        timeout=min(30, max(1, cfg.get("timeout_seconds", 15))), allow_redirects=False)
                    status = response.status_code
                    if status != 200:
                        errors.append(("auth_error" if status in (401, 403) else "request_error",
                                       "NAVER_AUTH_ERROR" if status in (401, 403) else "NAVER_HTTP_ERROR"))
                        break
                    payload = response.json()
                    items = payload["items"]
                    total = payload["total"]
                    if not isinstance(items, list) or not isinstance(total, int) or total < 0:
                        raise ValueError("invalid_payload")
                    times = []
                    for item in items:
                        try:
                            published = parsedate_to_datetime(item["pubDate"])
                            if published.tzinfo is None:
                                raise ValueError("missing_timezone")
                            published = published.astimezone(timezone.utc)
                            times.append(published)
                            if not start <= published <= end:
                                continue
                            selected = None
                            # An invalid original URL cannot fall back to a different publisher URL.
                            raw_url = item.get("originallink") or item.get("link")
                            for source in sources:
                                try:
                                    selected = safe_public_url(raw_url, source.get("hosts", source.get("allowed_hosts", [])))
                                except (ValueError, TypeError):
                                    continue
                                break
                            if selected is None:
                                continue
                            title, description = clean_text(item["title"]), clean_text(item.get("description"))
                            if not title:
                                raise ValueError("missing_title")
                            found[selected] = {"article_id": hashlib.sha256(selected.encode()).hexdigest(),
                                "url": selected, "title": title, "description": description,
                                "source_id": source["source_id"], "source_name": source.get("name", source["source_id"]),
                                "source_published_at": published.isoformat(), "published_at_basis": "naver_pubDate",
                                "evidence_scope": "description" if description else "title"}
                        except (KeyError, TypeError, ValueError, OverflowError):
                            invalid += 1
                    # Every date must be parsed before using the oldest timestamp to stop pagination.
                    if len(items) < page_size or page * page_size + len(items) >= total or (len(times) == len(items) and times and max(times) < start):
                        break
                    if page == max_pages - 1:
                        saturated = True
                except (ValueError, KeyError, TypeError):
                    errors.append(("parse_error", "NAVER_PARSE_ERROR"))
                    break
                except Exception:
                    # Never retain a raw HTTP exception: it may contain credential headers.
                    errors.append(("request_error", "NAVER_REQUEST_ERROR"))
                    break
        articles = sorted(found.values(), key=lambda item: (item["source_published_at"], item["url"]), reverse=True)
        source_results = []
        for source in sources:
            count = sum(a["source_id"] == source["source_id"] for a in articles)
            if errors or invalid:
                status = "partial" if count else (errors[0][0] if errors else "parse_error")
                code = errors[0][1] if errors else "NAVER_ITEM_PARSE_ERROR"
            elif saturated:
                status, code = "saturated", "NAVER_SEARCH_SATURATED"
            else:
                status, code = ("success" if count else "success_zero"), None
            source_results.append({"source_id": source["source_id"], "status": status, "count": count,
                "saturated": saturated, "invalid_items": invalid, **({"error_code": code} if code else {})})
        return {"articles": articles, "source_results": source_results}


def collect(config, start_at=None, end_at=None, transport=None):
    batch = Collector(config, transport=transport).collect(start_at, end_at)
    batch["source_status"] = batch["source_results"]
    return batch
