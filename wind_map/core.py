"""Exact values, explicit date precision and stable serialization."""
from __future__ import annotations
import calendar
import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

class WindError(ValueError):
    pass

def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

def stable_id(prefix, value):
    return prefix + "_" + hashlib.sha256(canonical(value)).hexdigest()[:24]

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        raise WindError("Timestamp requires an explicit timezone") from None

def day(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise WindError("Invalid ISO date") from None

def capacity_w(value, unit=None):
    if isinstance(value, (float, bool)):
        raise WindError("Capacity must be exact decimal text or an integer")
    text = str(value).strip().replace(",", "")
    match = re.fullmatch(r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(만)?\s*(MW|kW|W)?", text, re.I)
    if not match:
        raise WindError("Unrecognized capacity")
    suffix = (match[3] or unit or "").lower()
    if suffix not in {"mw", "kw", "w"}:
        raise WindError("Capacity unit is required")
    if match[3] and unit and match[3].lower() != unit.lower():
        raise WindError("Conflicting capacity units")
    amount = Decimal(match[1]) * (10000 if match[2] else 1) * {"mw": 1000000, "kw": 1000, "w": 1}[suffix]
    if amount != amount.to_integral_value() or amount > 9007199254740991:
        raise WindError("Capacity is not representable as safe integer watts")
    return int(amount)

def watts(value):
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise WindError("Expected non-negative integer watts")
    return value

def date_value(value=None, *, end=None, precision=None, estimated=False):
    if value is None:
        return {"start": None, "end": None, "precision": "unknown", "estimated": bool(estimated)}
    text = str(value).strip()
    if re.fullmatch(r"\d{4}", text):
        lo, hi, guessed = text + "-01-01", text + "-12-31", "year"
    elif re.fullmatch(r"\d{4}-\d{2}", text):
        y, m = map(int, text.split("-"))
        try:
            n = calendar.monthrange(y, m)[1]
        except (ValueError, calendar.IllegalMonthError):
            raise WindError("Invalid month") from None
        lo, hi, guessed = text + "-01", f"{text}-{n:02}", "month"
    else:
        lo, hi, guessed = text, end or text, "range" if end and end != text else "day"
    day(lo); day(hi)
    if lo > hi:
        raise WindError("Reversed date range")
    p = precision or guessed
    if p not in {"day", "month", "year", "range", "before", "after"}:
        raise WindError("Invalid date precision")
    if p == "day" and lo != hi:
        raise WindError("Day precision cannot cover multiple dates")
    return {"start": lo, "end": hi, "precision": p, "estimated": bool(estimated)}

def validate_date(value):
    if not isinstance(value, dict) or set(value) != {"start", "end", "precision", "estimated"} or type(value["estimated"]) is not bool:
        raise WindError("Date must preserve its precision with only public date fields")
    if value.get("precision") == "unknown":
        if value.get("start") is not None or value.get("end") is not None:
            raise WindError("Unknown dates cannot contain invented dates")
        return value
    checked = date_value(value.get("start"), end=value.get("end"), precision=value.get("precision"), estimated=value.get("estimated", False))
    if value["precision"] == "month" and value != date_value(value["start"][:7], estimated=value["estimated"]):
        raise WindError("Month precision must cover exactly that month")
    if value["precision"] == "year" and value != date_value(value["start"][:4], estimated=value["estimated"]):
        raise WindError("Year precision must cover exactly that year")
    if value["precision"] in {"before", "after"} and value["start"] != value["end"]:
        raise WindError("Open date bounds require one explicit boundary")
    if checked != value:
        raise WindError("Non-canonical date")
    return checked

def confirmed_by(value, as_of):
    validate_date(value)
    return bool(value["precision"] != "after" and value["end"] and value["end"] <= as_of)

def safe_url(value, hosts=None):
    if not isinstance(value, str) or len(value) > 4096:
        raise WindError("Invalid source URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise WindError("Invalid source URL") from None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or port not in (None, 443):
        raise WindError("Source URLs must use public HTTPS")
    host = parts.hostname.lower()
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WindError("Literal IP source URLs are not allowed")
    if host == "localhost" or "." not in host or host.endswith((".local", ".internal")):
        raise WindError("Non-public source URL")
    if hosts and host not in hosts:
        raise WindError("Source host is not configured")
    if any(k in parts.query.lower() for k in ("api_key=", "apikey=", "token=", "password=", "secret=")):
        raise WindError("Credentials cannot appear in public URLs")
    return value

def atomic_write(path, content):
    import os
    import tempfile
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".wind-map-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)