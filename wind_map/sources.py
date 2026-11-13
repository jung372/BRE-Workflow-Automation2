"""Explicitly configured official sources; all responses remain private candidates."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlencode, urljoin, urlsplit
import requests
from bs4 import BeautifulSoup
from .core import WindError, safe_url, stable_id, utc_now
from .queue import SearchQueue

class SourceClient:
    def __init__(self, hosts, *, session=None, interval=1.0, max_bytes=40_000_000):
        if not hosts:
            raise WindError("Explicit source hosts required")
        self.hosts = hosts
        self.session = session or requests.Session()
        self.interval, self.max_bytes, self.last = interval, max_bytes, 0
        self.session.headers.update({"User-Agent": "BRE-WindMap/1.0 (public-document research)", "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"})

    def fetch(self, url, *, method="GET", data=None):
        safe_url(url, self.hosts)
        for redirect in range(5):
            host = urlsplit(url).hostname
            if any(not ipaddress.ip_address(address[4][0]).is_global for address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)):
                raise WindError("Source DNS resolved to a non-public address")
            delay = self.interval - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()
            response = self.session.request(method, url, data=data, timeout=(10, 40), verify=True, allow_redirects=False, stream=True)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = safe_url(urljoin(url, response.headers.get("Location", "")), self.hosts)
                    if response.status_code in {301, 302, 303}:
                        method, data = "GET", None
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise WindError("Source attachment exceeds size limit")
                    chunks.append(chunk)
                content = b"".join(chunks)
                mime = response.headers.get("Content-Type", "")
                return {"content": content, "url": url, "mime": mime, "checked_at": utc_now()}
            finally:
                response.close()
        raise WindError("Too many source redirects")


def korec_detail_links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for row in soup.select("tbody tr"):
        number = row.select_one('input[name="bbsSntncNo"]')
        section = row.select_one('input[name="bbsSe"]')
        title = row.select_one("a")
        if number and number.get("value", "").isdigit():
            ident = number["value"]; category = section.get("value", "0") if section else "0"
            url = urljoin(base, "/notice/moveNoticeDetail.do?") + urlencode({"bbsSntncNo": ident, "bbsSe": category})
            links[url] = {"id": "korec-" + ident + "-" + category, "title": title.get_text(" ", strip=True) if title else row.get_text(" ", strip=True),
                          "url": url, "pinned": bool(row.select_one(".badge-category"))}
    for node in soup.find_all(["a", "tr"]):
        raw = " ".join(str(node.get(k, "")) for k in ("href", "onclick"))
        match = re.search(r"(?:rtn|fn|go|move)\w*\(['\"]?(\d+)['\"]?\s*,\s*['\"]?(\d+)", raw)
        if match:
            url = urljoin(base, "/notice/moveNoticeDetail.do?bbsSntncNo=" + match[1] + "&bbsSe=" + match[2])
            links[url] = {"id": "korec-" + match[1] + "-" + match[2], "title": node.get_text(" ", strip=True), "url": url}
        elif "/notice/moveNoticeDetail.do?" in raw:
            href = node.get("href", "")
            if href and not href.startswith("javascript:"):
                url = safe_url(urljoin(base, href), ["www.korec.go.kr"])
                links[url] = {"id": stable_id("korec", url), "title": node.get_text(" ", strip=True), "url": url}
    return list(links.values())


def korec_attachments(html, detail_url):
    soup = BeautifulSoup(html, "html.parser")
    paths = soup.select('input[name="filePath"], input[id="filePath"]')
    names = soup.select('input[name="fileName"], input[id="fileName"]')
    if len(paths) != len(names):
        raise WindError("Unpaired KOREC attachment fields")
    result = []
    seen = set()
    for path, name in zip(paths, names):
        params = {"attachingFilePath": path.get("value", ""), "attachingFileNm": name.get("value", "")}
        if not all(params.values()):
            continue
        url = urljoin(detail_url, "/commonfile/fileDownLoad.do?") + urlencode(params)
        if url not in seen:
            seen.add(url)
            result.append({"name": params["attachingFileNm"], "url": safe_url(url, ["www.korec.go.kr"])})
    return result


def decode_html(content):
    # BeautifulSoup detects HTML charset; requests' ISO-8859-1 fallback corrupts filenames.
    return str(BeautifulSoup(content, "html.parser", from_encoding="utf-8"))


def count_from_html(soup, config):
    selector = config.get("total_selector")
    if not selector:
        raise WindError("Verified total selector is required")
    nodes = soup.select(selector)
    if len(nodes) != 1:
        raise WindError("Source total selector changed")
    match = re.search(config.get("total_pattern", r"([\d,]+)"), nodes[0].get_text(" ", strip=True))
    if not match:
        raise WindError("Source result count is unavailable")
    return int(match[1].replace(",", ""))


def parse_html_page(html, config, job):
    soup = BeautifulSoup(html, "html.parser")
    total = count_from_html(soup, config)
    items = []
    if config["kind"] == "korec":
        items = [r for r in korec_detail_links(html, config["url"]) if not r.get("pinned")]
    elif config["kind"] == "eiass":
        scope = config.get("assessment_type")
        if scope not in {"main", "small", "strategic"}:
            raise WindError("EIASS assessment scope must be explicit")
        for tr in soup.select(config.get("row_selector", "tbody tr")):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells or not re.fullmatch(r"[A-Z]{2}\d{8}", cells[0]):
                continue
            # Keep every stage/round, not only the newest row per business code.
            items.append({"id": stable_id("eiass", [scope, cells]), "business_code": cells[0],
                          "assessment_type": scope, "cells": cells, "url": config["url"], "status": "candidate"})
    else:
        for row in soup.select(config["row_selector"]):
            link = row.select_one(config["link_selector"])
            if not link or not link.get("href"):
                raise WindError("Notice detail link is missing")
            url = safe_url(urljoin(config["url"], link["href"]), config["hosts"])
            items.append({"id": stable_id("notice", url), "title": link.get_text(" ", strip=True),
                          "text": row.get_text(" ", strip=True), "url": url, "status": "candidate"})
    size = config.get("page_size", 20)
    if type(size) is not int or size < 1:
        raise WindError("Invalid page size")
    last = job["cursor"] * size >= total
    supplemental = [r for r in korec_detail_links(html, config["url"]) if r.get("pinned")] if config["kind"] == "korec" else []
    return {"items": items, "supplemental_items": supplemental, "total": total, "last_page": last,
            "complete_proof": "source_total" if last else None, "checked_at": utc_now()}


def parse_open_portal(payload, *, query, cursor, page_size):
    # Authentication shells and access errors cannot become an empty successful page.
    if not isinstance(payload, dict) or not isinstance(payload.get("rtnList"), list):
        raise WindError("Information portal result list unavailable")
    rows = payload["rtnList"]
    raw_total = payload.get("rtnTotal", payload.get("totalCnt", payload.get("totalCount")))
    if raw_total is None:
        raise WindError("Information portal result count unavailable")
    total = int(str(raw_total).replace(",", ""))
    items = []
    for row in rows:
        ident = row.get("PRDCTN_INSTT_REGIST_NO")
        if not ident:
            raise WindError("Information portal stable document ID unavailable")
        # Do not ingest contact names or authentication/session fields.
        items.append({"id": str(ident), "title": row.get("INFO_SJ", ""), "document_number": row.get("DOC_NO", ""),
                      "date": row.get("P_DATE", ""), "agency": row.get("ALL_PROC_INSTT_NM", ""),
                      "visibility": "metadata_only", "status": "candidate", "query": query})
    last = cursor * page_size >= total
    return {"items": items, "total": total, "last_page": last, "complete_proof": "source_total" if last else None, "checked_at": utc_now()}


def fetch_search_page(config, job, client):
    params = dict(config.get("params", {}))
    params.update({config.get("query_param", "query"): job["query"],
                   config.get("page_param", "currentPage"): str(job["cursor"])})
    if config.get("start_param"):
        params[config["start_param"]] = job["start"].replace("-", "") if config.get("compact_dates") else job["start"]
    if config.get("end_param"):
        params[config["end_param"]] = job["end"].replace("-", "") if config.get("compact_dates") else job["end"]
    if (not config.get("start_param") or not config.get("end_param")) and not (config["kind"] == "korec" and config.get("window_mode") == "full_archive"):
        raise WindError("Backfill source must enforce the requested date window")
    method = config.get("method", "GET")
    if method == "GET":
        url = config["url"] + ("&" if "?" in config["url"] else "?") + urlencode(params)
        result = client.fetch(url)
    elif method == "POST":
        result = client.fetch(config["url"], method="POST", data=params)
    else:
        raise WindError("Unsupported source method")
    page = parse_html_page(decode_html(result["content"]), config, job)
    return page, result


def run_job(store, configs, *, max_pages=10, fetcher=None):
    queue = SearchQueue(store)
    job = queue.claim(source_ids=configs.keys())
    if not job:
        return {"status": "idle"}
    try:
        config = configs[job["source"]]
        if job["data"].get("kind") == "notice_detail":
            result = ingest_notice(store, job["query"], agency=config["agency"], hosts=config["hosts"], title=job["data"].get("title"))
            ids = [result["detail_id"], *result["attachment_ids"]]
            queue.checkpoint(job, {"items":[{"id":i} for i in sorted(set(ids))], "total":len(set(ids)), "last_page":True, "complete_proof":"detail_and_all_attachments"})
            return {"id":job["id"], "status":"done", "documents":len(set(ids))}
        client = SourceClient(config["hosts"]) if fetcher is None else None
        for _ in range(max_pages):
            page, raw = fetcher(config, job) if fetcher else fetch_search_page(config, job, client)
            doc_id = store.register_document(raw["content"], {"source_key": f'{job["source"]}:{job["id"]}:{job["cursor"]}',
                "agency": config["agency"], "title": "검색 결과 " + job["query"], "url": raw["url"],
                "checked_at": raw["checked_at"], "visibility": "restricted", "format": "html",
                "query": job["query"], "interval": [job["start"], job["end"]]})
            page["document_id"] = doc_id
            if queue.checkpoint(job, page):
                return {"id": job["id"], "status": "done"}
        # Cooperative checkpoint: another invocation continues the next page immediately.
        with store.transaction():
            queue._owned(job)
            store.db.execute("UPDATE queue SET status='pending',lease_until=NULL WHERE id=?", (job["id"],))
        return {"id": job["id"], "status": "pending"}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, WindError) else type(exc).__name__
        queue.fail(job, reason)
        return {"id": job["id"], "status": "blocked", "reason": reason}


class OpenPortalSession:
    """Caller explicitly owns browser lifetime. No proxy/TLS bypass and no automatic login."""
    def __init__(self, context, *, page_size=100):
        self.context, self.page_size = context, page_size

    def fetch(self, config, job):
        from urllib.parse import quote
        base = "https://www.open.go.kr"
        if config["url"] != base + "/othicInfo/infoList/infoList.ajax":
            raise WindError("Unexpected information-portal endpoint")
        page = self.context.new_page()
        try:
            page.goto(base + "/com/search/uniSrhMoreList.do?kwd=" + quote(job["query"]), wait_until="networkidle", timeout=45000)
            data = {"kwd": job["query"], "startDate": job["start"].replace("-", ""), "endDate": job["end"].replace("-", ""),
                    "pageIndex": str(job["cursor"]), "rowPage": str(self.page_size),
                    "recordCountPerPage": str(self.page_size), "pageUnit": str(self.page_size),
                    "reSrchFlag": "off", "detailSearch": "false"}
            response = self.context.request.post(config["url"], form=data, timeout=40000)
            if not response.ok:
                raise WindError("Information portal access failed")
            raw = response.body()
            if len(raw) > 20_000_000:
                raise WindError("Oversized portal response")
            parsed = parse_open_portal(json.loads(raw), query=job["query"], cursor=job["cursor"], page_size=self.page_size)
            return parsed, {"content": raw, "url": config["url"], "checked_at": utc_now()}
        finally:
            page.close()

def ingest_notice(store, url, *, agency, hosts, client=None, title=None, quarter=None):
    """Fetch a detail and every linked attachment; registration is not fact approval."""
    client = client or SourceClient(hosts)
    raw = client.fetch(safe_url(url, hosts))
    html = decode_html(raw["content"])
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".board-view-title, .board-view .tit, h3.head-tit, title")
    name = title or (heading.get_text(" ", strip=True) if heading else "공개 공고")
    detail = store.register_document(raw["content"], {"source_key":url, "agency":agency, "title":name,
        "url":url, "checked_at":raw["checked_at"], "visibility":"public", "format":"html"})
    if urlsplit(url).hostname == "www.korec.go.kr":
        attachments = korec_attachments(html, url)
    else:
        attachments = []
        for a in soup.select('a[href]'):
            href = urljoin(url, a["href"])
            if re.search(r"\.(pdf|hwpx?|zip)(?:[?#]|$)", href, re.I):
                attachments.append({"name":a.get_text(" ", strip=True) or "첨부", "url":safe_url(href, hosts)})
    ids = []
    for number, attachment in enumerate(attachments):
        value = client.fetch(attachment["url"])
        content = value["content"]
        meta = {"source_key":url + ":attachment:" + str(number), "agency":agency, "title":attachment["name"],
                "url":attachment["url"], "checked_at":value["checked_at"], "visibility":"public",
                "parent_document_id":detail, "attachment_name":attachment["name"]}
        if content.startswith(b"%PDF-"):
            import io
            import pdfplumber
            meta["format"] = "pdf"
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                meta["page_count"] = len(pdf.pages)
            if "발전사업허가대장" in attachment["name"].replace(" ", ""):
                meta["purpose"] = "permit_register"
                if quarter:
                    meta["quarter"] = quarter
        elif content.startswith(b"PK") and attachment["name"].lower().endswith(".hwpx"):
            meta["format"] = "hwpx"; meta["page_count"] = 1
        elif content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            meta["format"] = "hwp"
        else:
            raise WindError("Unexpected attachment format; no HTML error page accepted as a document")
        previous = [d for d in store.all("documents") if d["source_key"] == meta["source_key"]]
        if previous:
            meta["previous_version"] = max(previous, key=lambda d:d["checked_at"])["id"]
        ids.append(store.register_document(content, meta))
    return {"detail_id":detail, "attachment_ids":ids, "status":"candidates_only"}


def enqueue_project_searches(store, source_ids, end, *, earliest="2001-01-01", incremental=False):
    """Only approved public names/aliases are sent to public search providers."""
    from .snapshot import public_evidence
    docs = {d["id"]: d for d in store.all("documents")}
    evidence = {e["id"]: e for e in store.all("evidence")}
    events = store.all("events")
    permits = store.all("permits")
    queue = SearchQueue(store)
    jobs = set()
    for project in store.all("projects"):
        if project.get("wind_type") != "onshore":
            continue
        terms = [(project["name"], project["name_evidence_id"])]
        terms += [(a["name"], a["evidence_id"]) for a in project.get("aliases", [])]
        terms += [(t["value"], t["evidence_id"]) for t in project.get("search_terms", [])]
        ids = {p["id"] for p in permits if p["project_id"] == project["id"]}
        starts = [e["date"]["start"] for e in events if e["permit_id"] in ids and e.get("is_initial") is True and e.get("review_state") == "approved" and e["date"]["start"]]
        start = min(starts) if starts else earliest
        for query, source in terms:
            public_evidence(evidence[source], docs)
            if not isinstance(query, str) or len(query.strip()) < 2:
                raise WindError("Search term is too short")
            for source_id in source_ids:
                if incremental:
                    jobs.update(queue.incremental(source_id, query, end, earliest=start))
                else:
                    jobs.update(queue.backfill(source_id, query, start, end, metadata={"project_id":project["id"]}))
    return sorted(jobs)
