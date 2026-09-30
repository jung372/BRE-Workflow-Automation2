"""Bounded discovery and article extraction from public publisher pages.

No API account, paid-content identifiers, authentication or paywall bypass.
Accessibility is established from an ordinary successful article-page response.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import re
import time
from urllib.parse import parse_qs, urljoin, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .collector import _instant, clean_text, safe_public_url


class SourceError(Exception):
    pass


def parse_article(content, source, url):
    soup = BeautifulSoup(content, "html.parser")
    body = soup.select_one(source["body_selector"])
    title = soup.select_one('meta[property="og:title"]')
    published = soup.select_one('meta[property="article:published_time"]')
    if not body or not title or not published:
        raise SourceError("ARTICLE_PARSE_FAILED")
    headline = clean_text(title.get("content", ""))
    if re.match(r"[\[(]?(인사|부고|동정)[\])]?", headline):
        return None
    # Check the article area, not a navigation bar advertising subscriptions.
    if body.select('form[action*="login"], .paywall, .premium-lock, .article-paywall'):
        raise SourceError("ARTICLE_ACCESS_RESTRICTED")
    for node in body.select("script, style, iframe, figure, .ad, .article-copyright, .reporter-area"):
        node.decompose()
    text = clean_text(body.get_text(" ", strip=True))
    if re.search(r"(로그인|구독|결제|유료회원).{0,30}(후.{0,10}(기사|본문)|전용|읽을 수|이용할 수)|유료.{0,8}기사입니다", text):
        raise SourceError("ARTICLE_ACCESS_RESTRICTED")
    if len(text) < 100:
        raise SourceError("ARTICLE_BODY_INCOMPLETE")
    if not headline:
        raise SourceError("ARTICLE_PARSE_FAILED")
    if "풍력" not in headline + " " + text:
        return None
    # Personnel notices mentioning a wind division are not industry news.
    if re.match(r"[\[(]?(인사|부고|동정)[\])]?", headline):
        return None
    published_at = _instant(published.get("content", ""))
    return {"article_id": hashlib.sha256(url.encode()).hexdigest(), "url": url,
        "title": headline.removesuffix(" | 연합뉴스"), "description": "", "text": text,
        "source_id": source["source_id"], "source_name": source["name"],
        "source_published_at": published_at.isoformat(), "published_at_basis": "publisher_article_meta",
        "evidence_scope": "full_text", "access_status": "free", "is_paywalled": False,
        "access_basis": "public_article_body", "access_checked_at": datetime.now(timezone.utc).isoformat()}


class PublicCollector:
    def __init__(self, config, transport=None, clock=None, sleeper=None):
        self.config = config
        self.resolve = transport is None
        if transport is None:
            import requests
            transport = requests.Session()
            transport.trust_env = False
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleeper or time.sleep

    def fetch(self, url, source):
        for _ in range(4):
            try:
                url = safe_public_url(url, source["hosts"], resolve=self.resolve)
            except ValueError as exc:
                raise SourceError("SOURCE_URL_REJECTED") from exc
            self.sleep(max(0, min(2, self.config.get("request_interval_seconds", .4))))
            response = self.transport.get(url, timeout=min(30, self.config.get("timeout_seconds", 15)),
                headers={"User-Agent": "BRE-Wind-Briefing/1.0 (public news reader)"},
                allow_redirects=False, stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status_code != 200:
                    raise SourceError("SOURCE_ACCESS_DENIED" if response.status_code in (401, 403, 402) else "SOURCE_HTTP_ERROR")
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise SourceError("SOURCE_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                return b"".join(chunks), url
            finally:
                response.close()
        raise SourceError("SOURCE_REDIRECT_LIMIT")

    def discover(self, source, start, end):
        links, seen, saturated = [], set(), False
        if source["discovery"] == "rss":
            for url in source["feed_urls"]:
                content, _ = self.fetch(url, source)
                root = ET.fromstring(content)
                if root.tag != "rss" or root.find("channel") is None:
                    raise SourceError("FEED_PARSE_FAILED")
                for item in root.findall("./channel/item"):
                    if not all(item.findtext(field) for field in ('title', 'link', 'pubDate')):
                        raise SourceError("FEED_ITEM_PARSE_FAILED")
                    if "풍력" not in clean_text(" ".join(item.itertext())):
                        continue
                    published = parsedate_to_datetime(item.findtext("pubDate", ""))
                    if published.tzinfo is None:
                        raise SourceError("FEED_DATE_INVALID")
                    if not start <= published <= end:
                        continue
                    link = item.findtext("link", "")
                    if link not in seen:
                        seen.add(link); links.append(link)
        elif source["discovery"] == "ndsoft_list":
            pages = min(10, max(1, self.config.get("max_pages", 5)))
            url = source["list_url"]
            page_fingerprints = set()
            for page in range(1, pages + 1):
                content, final = self.fetch(url, source)
                soup = BeautifulSoup(content, "html.parser")
                section = soup.select_one("#section-list")
                if section is None:
                    raise SourceError("LIST_PARSE_FAILED")
                anchors = section.select('a[href*="articleView.html?"]')
                if not anchors:
                    if re.search(r"검색결과가 없습니다|기사가 없습니다", section.get_text()):
                        break
                    raise SourceError("LIST_PARSE_FAILED")
                fingerprint = tuple(sorted({a['href'] for a in anchors}))
                if fingerprint in page_fingerprints:
                    saturated = True
                    break
                page_fingerprints.add(fingerprint)
                for anchor in anchors:
                    if not re.search(r"풍력|재생에너지|해상|터빈|해저케이블", anchor.get_text()):
                        continue
                    link = urljoin(final, anchor["href"])
                    if link not in seen:
                        seen.add(link); links.append(link)
                # List pages have only abbreviated dates. The article timestamp
                # is authoritative; reaching this finite limit means partial coverage.
                if page == pages:
                    saturated = True
                next_page = next((a['href'] for a in soup.select('a[href]')
                    if 'articleList.html' in a['href'] and
                    parse_qs(urlsplit(a['href']).query).get('page') == [str(page + 1)]), None)
                if not next_page:
                    break
                url = urljoin(final, next_page)
        else:
            raise SourceError("DISCOVERY_UNSUPPORTED")
        limit = min(100, max(1, self.config.get("max_articles_per_source", 30)))
        return links[:limit], saturated or len(links) > limit

    def collect(self, since=None, until=None):
        end = _instant(until or self.clock())
        start = _instant(since) if since else end - timedelta(hours=72)
        articles, results = [], []
        for source in self.config.get("sources", []):
            if not source.get("enabled", True):
                continue
            accepted, errors, excluded, saturated = [], [], 0, False
            try:
                links, saturated = self.discover(source, start, end)
                for url in links:
                    try:
                        content, final = self.fetch(url, source)
                        article = parse_article(content, source, final)
                        if article and start <= _instant(article["source_published_at"]) <= end:
                            accepted.append(article)
                    except SourceError as exc:
                        if str(exc) == "ARTICLE_ACCESS_RESTRICTED":
                            excluded += 1
                        else:
                            errors.append(str(exc))
                    except (ValueError, KeyError, TypeError):
                        errors.append("ARTICLE_PARSE_FAILED")
                    except Exception:
                        errors.append("ARTICLE_REQUEST_OR_PARSE_FAILED")
            except SourceError as exc:
                errors.append(str(exc))
            except (ValueError, KeyError, TypeError, ET.ParseError):
                errors.append("SOURCE_PARSE_FAILED")
            except Exception:
                errors.append("SOURCE_REQUEST_OR_PARSE_FAILED")
            failure = "parse_error" if errors and any("PARSE" in e or "INCOMPLETE" in e for e in errors) else "request_error"
            status = ("partial" if accepted else failure) if errors else "saturated" if saturated else "success" if accepted else "success_zero"
            results.append({"source_id": source["source_id"], "status": status, "count": len(accepted),
                "excluded_restricted": excluded, **({"error_code": errors[0]} if errors else {})})
            articles.extend(accepted)
        return {"articles": list({a["url"]: a for a in articles}.values()), "source_results": results}
