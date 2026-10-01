"""Public news-search link discovery followed by publisher-only extraction.

Search snippets and search dates are not evidence. No account, search API key,
paywall bypass, or search-result text is sent to the summarizer.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta, timezone
import json
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .collector import _instant, safe_public_url
from .public_sources import PublicCollector, SourceError, parse_article

SEARCH_HOSTS = ["search.naver.com", "s.search.naver.com"]
NON_PUBLISHERS = {"naver.com", "google.com", "youtube.com", "facebook.com", "instagram.com"}


def publisher_url(value, resolve=False):
    parts = urlsplit(value)
    # A search link may advertise HTTP even when its publisher serves HTTPS.
    # Never fall back to HTTP or disable certificate verification.
    if parts.scheme == "http":
        value = urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))
    host = (parts.hostname or "").lower().rstrip(".")
    if any(host == h or host.endswith("." + h) for h in NON_PUBLISHERS):
        raise ValueError("not_publisher")
    value = safe_public_url(value, [host], resolve=resolve)
    parts = urlsplit(value)
    query = [(k,v) for k,v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def search_page(content):
    """Read both initial HTML and the next-page URL supplied by that page."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    next_url = None
    if text.lstrip().startswith("{"):
        payload = json.loads(text)
        sections = payload["collection"]
        if not isinstance(sections, list):
            raise SourceError("SEARCH_PARSE_FAILED")
        text = "".join(c.get("html", "") for c in sections)
        next_url = payload.get("url") or None
    else:
        match = re.search(r'"url":\s*("https[^"\n]+")\s*,\s*"insertTarget"', text)
        if match:
            next_url = json.loads(match[1])
    soup = BeautifulSoup(text, "html.parser")
    anchors = soup.select('a[data-heatmap-target=".tit"], a.news_tit')
    links = []
    for anchor in anchors:
        try:
            url = publisher_url(anchor.get("href", ""))
            if url not in links: links.append(url)
        except ValueError:
            continue
    if not anchors and not re.search(r"검색결과가 없습니다|검색 결과가 없습니다|검색결과를 찾지 못", soup.get_text()):
        raise SourceError("SEARCH_PARSE_FAILED")
    if next_url:
        next_url = safe_public_url(next_url, SEARCH_HOSTS)
        if urlsplit(next_url).path != "/p/newssearch/3/api/tab/more":
            raise SourceError("SEARCH_NEXT_URL_REJECTED")
    return links, next_url


class SearchCollector(PublicCollector):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host_lock = threading.Lock()
        self.host_next = {}

    def fetch_publisher(self, url):
        for _ in range(4):
            url = publisher_url(url, resolve=self.resolve)
            host = urlsplit(url).hostname
            with self.host_lock:
                now = time.monotonic()
                delay = max(0, self.host_next.get(host, 0) - now)
                self.host_next[host] = now + delay + .5
            self.sleep(delay)
            # One hop at a time: every new destination is checked before I/O.
            response = self.transport.get(url, timeout=min(15, self.config.get("timeout_seconds", 12)),
                headers={"User-Agent": "BRE-Wind-Briefing/1.0 (public news reader)"},
                allow_redirects=False, stream=True)
            try:
                if response.status_code in (301,302,303,307,308):
                    url = urljoin(url, response.headers.get("Location", "")); continue
                if response.status_code != 200:
                    raise SourceError("ARTICLE_ACCESS_RESTRICTED" if response.status_code in (401,402,403) else "ARTICLE_HTTP_ERROR")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2_000_000: raise SourceError("SOURCE_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                return b"".join(chunks), url
            finally:
                response.close()
        raise SourceError("SOURCE_REDIRECT_LIMIT")

    def collect_source(self, source, start, end):
        queries = self.config.get("queries", [])
        maximum = min(400, max(10, self.config.get("search_max_articles", 240)))
        page_limit = min(10, max(1, self.config.get("search_pages_per_query", 3)))
        # Query every theme once before spending remaining pages on any theme.
        pending = []
        kst = timezone(timedelta(hours=9))
        for query in queries:
            params = {"where":"news", "query":query, "sort":"1", "pd":"3",
                      "ds":start.astimezone(kst).strftime("%Y.%m.%d"),
                      "de":end.astimezone(kst).strftime("%Y.%m.%d")}
            pending.append((query, "https://search.naver.com/search.naver?"+urlencode(params), 1))
        found, fingerprints, query_stats = {}, {}, {}
        errors, outcomes = Counter(), Counter()
        saturated = False
        while pending:
            query, url, page = pending.pop(0)
            stat = query_stats.setdefault(query, {"pages":0,"links":0,"status":"success"})
            try:
                data, _ = self.fetch(url, {"hosts":SEARCH_HOSTS})
                links, next_url = search_page(data)
                fingerprint = tuple(links)
                if fingerprint in fingerprints.setdefault(query, set()):
                    stat["status"] = "repeated_page"; saturated = True; continue
                fingerprints[query].add(fingerprint)
                stat["pages"] += 1; stat["links"] += len(links)
                for link in links: found.setdefault(link, query)
                if next_url:
                    if page < page_limit: pending.append((query,next_url,page+1))
                    else: stat["status"] = "saturated"; saturated = True
            except Exception as exc:
                code = str(exc) if isinstance(exc, SourceError) else "SEARCH_REQUEST_OR_PARSE_FAILED"
                errors[code] += 1; stat["status"] = code
        links = list(found)
        if len(links) > maximum: saturated = True
        publisher_stats = {}
        def extract(url):
            try:
                data, final = self.fetch_publisher(url)
                article = parse_article(data, source, final)
                if article is None: return url, None, "irrelevant"
                if not start <= _instant(article["source_published_at"]) <= end:
                    return url, None, "outside_window"
                return url, article, "accepted"
            except SourceError as exc: return url, None, str(exc)
            except Exception: return url, None, "ARTICLE_REQUEST_OR_PARSE_FAILED"
        # Only network and parsing run concurrently. DB still has one writer.
        articles = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for url, article, outcome in pool.map(extract, links[:maximum]):
                outcomes[outcome] += 1
                host = urlsplit(url).hostname
                stats = publisher_stats.setdefault(host, Counter())
                stats[outcome] += 1
                if article: articles.append(article)
        failures = sum(v for k,v in outcomes.items() if k not in {"accepted","irrelevant","outside_window","ARTICLE_ACCESS_RESTRICTED"})
        status = "partial" if errors or failures else "saturated" if saturated else "success" if articles else "success_zero"
        if not articles and (errors or failures): status = "request_error"
        result = {"source_id":source["source_id"],"status":status,"count":len(articles),
                  "discovered":len(links),"attempted":min(len(links),maximum),
                  "excluded_restricted":outcomes["ARTICLE_ACCESS_RESTRICTED"],
                  "query_results":query_stats,"publisher_results":publisher_stats,"outcomes":outcomes}
        if errors: result["error_code"] = next(iter(errors))
        return {"articles":articles,"source_results":[result]}
