"""One-off read-only portal diagnostics; never print cookies or document bodies."""
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

release = Path(sys.argv[1])
sys.path.insert(0, str(release))
from runtime_config import ensure_utf8_stdout, load_app_environment
ensure_utf8_stdout()
load_app_environment()
from config import playwright_proxy
from keywords import load_keywords
from scrapers.open_portal import LIST_API, USER_AGENT, _payload, search_url
from playwright.sync_api import sync_playwright


def summary(raw):
    try:
        value = json.loads(raw)
    except ValueError:
        return {"json": False, "length": len(raw), "html": "<html" in raw.lower()}
    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    result = value.get("result")
    return {"keys": list(value), "result_keys": list(result) if isinstance(result, dict) else None,
            "rows": len(result["rtnList"]) if isinstance(result, dict) and isinstance(result.get("rtnList"), list) else None,
            "total": result.get("rtnTotal") if isinstance(result, dict) else None,
            "scalar_status": {k: v for k, v in value.items() if k.lower() in ("code", "status", "errorcode", "success") and isinstance(v, (str, int, bool))}}


cfg = load_keywords()
today = datetime.now().date()
data = _payload(cfg.keywords[0], cfg, (today - timedelta(days=cfg.window_days)).strftime("%Y%m%d"), (today + timedelta(days=1)).strftime("%Y%m%d"))
with sync_playwright() as p:
    options = {"headless": True}
    proxy = playwright_proxy()
    if proxy:
        options["proxy"] = proxy
    print("proxy_configured", bool(proxy), flush=True)
    browser = p.chromium.launch(**options)
    try:
        context = browser.new_context(user_agent=USER_AGENT, locale="ko-KR", extra_http_headers={"Accept-Language": "ko-KR,ko;q=0.9"})
        page = context.new_page()
        observed = []
        def observe(response):
            if response.request.resource_type in ("xhr", "fetch"):
                observed.append(response)
        page.on("response", observe)
        page.on("pageerror", lambda error: print("pageerror", str(error)[:300], flush=True))
        response = page.goto(search_url(cfg.keywords[0]), wait_until="networkidle", timeout=40000)
        page.wait_for_timeout(2000)
        print("shell", response.status, "title", page.title(), "path", page.url.split("?")[0], flush=True)
        print("script_urls", page.locator("script[src]").evaluate_all("nodes => nodes.map(n=>n.src)"), flush=True)
        print("navigation", page.locator("a[href]").evaluate_all("nodes => nodes.filter(n=>/정보목록|검색|전체보기/.test(n.textContent)).map(n=>({text:n.textContent.trim().slice(0,80),href:n.getAttribute('href')})).slice(0,50)"), flush=True)
        print("forms", page.locator("form").evaluate_all("nodes=>nodes.map(n=>({id:n.id,action:n.action,fields:Array.from(n.elements).map(e=>({name:e.name,type:e.type}))}))"), flush=True)
        print("frames", [frame.url for frame in page.frames], flush=True)
        print("shell_text", page.locator("body").inner_text()[:3500], flush=True)
        cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies()}
        print("cookie_names", list(cookies), flush=True)
        for captured in observed:
            try:
                print("page_response", captured.url.split("?")[0], captured.status, summary(captured.text()), flush=True)
            except Exception:
                print("page_response", captured.url.split("?")[0], captured.status, "body unavailable", flush=True)
        for script in page.locator("script").all_text_contents():
            endpoints = re.findall(r'''['"]([^'"\r\n]*(?:\.ajax|\.do|\.js)[^'"\r\n]*)['"]''', script)
            if endpoints:
                print("inline_endpoints", endpoints, flush=True)
        scripts = page.locator("script[src]").evaluate_all("nodes => nodes.map(n=>n.src).filter(u=>!/jquery|bxslider|jqcloud/i.test(u))")
        for url in scripts:
            source = context.request.get(url).text()
            endpoints = re.findall(r'''['"]([^'"\r\n]*(?:\.ajax|\.do)[^'"\r\n]*)['"]''', source)
            if endpoints:
                print("external_endpoints", url.split("?")[0], endpoints, flush=True)
            for match in re.finditer(r"infoList|setFingerPrint", source):
                print("external_script", url.split("?")[0], source[max(0,match.start()-250):match.end()+350], flush=True)
        headers = {"X-Requested-With": "XMLHttpRequest", "X-XSRF-TOKEN": cookies.get("XSRF-TOKEN", ""), "Referer": search_url(cfg.keywords[0])}
        for transport in ("request", "browser"):
            if transport == "request":
                result = context.request.post(LIST_API, form=data, headers=headers, timeout=40000)
                print(transport, result.status, summary(result.text()), flush=True)
            else:
                value = page.evaluate("""async ({url, data, headers}) => {
                    const response = await fetch(url, {method:'POST', headers: {...headers, 'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'}, body: new URLSearchParams(data)});
                    return {status:response.status, body:await response.text()};
                }""", {"url": LIST_API, "data": data, "headers": headers})
                print(transport, value["status"], summary(value["body"]), flush=True)
    finally:
        browser.close()
