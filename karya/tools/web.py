"""Web research: search, news, read any page."""
from __future__ import annotations

import io
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from ..registry import P, tool

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"}


TEXT_BACKENDS = ("duckduckgo", "yahoo", "google", "bing", "brave", "mojeek")
NEWS_BACKENDS = ("duckduckgo", "yahoo", "bing")


def _ddgs_text(query: str, max_results: int, region: str, timelimit: str | None):
    for backend in TEXT_BACKENDS:
        try:
            rows = DDGS(timeout=10).text(query, region=region, timelimit=timelimit or None,
                                         max_results=max_results, backend=backend)
        except Exception:
            continue
        if rows:
            return rows
    return []


def _ddgs_news(query: str, max_results: int, region: str, timelimit: str):
    for backend in NEWS_BACKENDS:
        try:
            rows = DDGS(timeout=10).news(query, region=region, timelimit=timelimit, max_results=max_results, backend=backend)
        except Exception:
            continue
        if rows:
            return rows
    return []


@tool("web_search", "Search the web. Returns titles, links and snippets.", {
    "query": P("string", "What to search for"),
    "max_results": P("integer", "Number of results (default 8)"),
    "timelimit": P("string", "Recency filter: d=day, w=week, m=month, y=year", enum=["d", "w", "m", "y"]),
    "region": P("string", "Region code, e.g. in-en (India), us-en, wt-wt (global). Default wt-wt"),
}, required=["query"], group="web")
def web_search(query: str, max_results: int = 8, timelimit: str | None = None, region: str = "wt-wt"):
    rows = _ddgs_text(query, max(1, min(max_results, 20)), region, timelimit)
    return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in rows] or "No results."


@tool("news_search", "Latest news articles about any topic (companies, markets, tech, world).", {
    "query": P("string", "News topic"),
    "max_results": P("integer", "Number of articles (default 8)"),
    "timelimit": P("string", "d=today, w=this week, m=this month (default w)", enum=["d", "w", "m"]),
    "region": P("string", "Region code, e.g. in-en, us-en, wt-wt. Default wt-wt"),
}, required=["query"], group="web")
def news_search(query: str, max_results: int = 8, timelimit: str = "w", region: str = "wt-wt"):
    rows = _ddgs_news(query, max(1, min(max_results, 25)), region, timelimit)
    return [{"date": (r.get("date") or "")[:16], "title": r.get("title"), "source": r.get("source"),
             "url": r.get("url"), "summary": (r.get("body") or "")[:300]} for r in rows] or "No news found."


def html_to_text(html: str, base_url: str = "") -> tuple[str, str, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    links = []
    for a in main.find_all("a", href=True)[:400]:
        label = a.get_text(" ", strip=True)
        href = requests.compat.urljoin(base_url, a["href"])
        if label and href.startswith("http") and len(label) > 3:
            links.append({"text": label[:80], "url": href})
    return title, text, links


@tool("fetch_url", "Open a web page (or PDF) and return its readable text and main links.", {
    "url": P("string", "Full URL starting with http(s)://"),
    "max_chars": P("integer", "Max characters of text to return (default 8000)"),
    "include_links": P("boolean", "Also return up to 30 links found on the page"),
}, required=["url"], group="web")
def fetch_url(url: str, max_chars: int = 8000, include_links: bool = False):
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    resp = requests.get(url, headers=HEADERS, timeout=25)
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(resp.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
        return {"url": resp.url, "status": resp.status_code, "type": "pdf", "text": text[:max_chars]}
    if "json" in ctype:
        return {"url": resp.url, "status": resp.status_code, "type": "json", "text": resp.text[:max_chars]}
    title, text, links = html_to_text(resp.text, resp.url)
    out = {"url": resp.url, "status": resp.status_code, "title": title, "text": text[:max_chars]}
    if len(text) > max_chars:
        out["note"] = f"text truncated ({len(text)} chars total)"
    if include_links:
        out["links"] = links[:30]
    if resp.status_code >= 400 or len(text) < 200:
        out["hint"] = "Page may need JavaScript or a login; try browser_open + browser_snapshot."
    return out



_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
_BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$|@(example|sentry|wixpress|domain|email)\.|^(name|user|your)@", re.I)
_SOCIAL = {"linkedin": "linkedin.com", "x": ("twitter.com", "x.com"), "instagram": "instagram.com", "youtube": "youtube.com",
           "facebook": "facebook.com", "github": "github.com", "tiktok": "tiktok.com", "discord": ("discord.gg", "discord.com"),
           "telegram": "t.me"}
CONTACT_PATHS = ("", "contact", "contact-us", "about", "about-us", "team", "support", "partners", "press")


def decode_cfemail(encoded: str) -> str:
    """Cloudflare hides emails as data-cfemail hex (first byte is an XOR key)."""
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return ""


def contacts_from_html(page_html: str, base_url: str) -> tuple[set[str], dict, set[str]]:
    soup = BeautifulSoup(page_html, "html.parser")
    emails = set()
    for tag in soup.select("[data-cfemail]"):
        emails.add(decode_cfemail(tag.get("data-cfemail", "")))
    for a in soup.select('a[href^="mailto:"]'):
        emails.add(a["href"][7:].split("?")[0].strip())
    for a in soup.select('a[href*="/cdn-cgi/l/email-protection#"]'):
        emails.add(decode_cfemail(a["href"].split("#", 1)[1]))
    emails.update(_EMAIL_RE.findall(soup.get_text(" ")))
    socials: dict[str, str] = {}
    pages: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = requests.compat.urljoin(base_url, a["href"])
        low = href.lower()
        for name, domains in _SOCIAL.items():
            for d in (domains if isinstance(domains, tuple) else (domains,)):
                if f"//{d}/" in low or f".{d}/" in low or f"//www.{d}/" in low:
                    if name not in socials and not re.search(r"/(share|intent|sharer|home)\b", low):
                        socials[name] = href.split("?")[0]
        if re.search(r"/(contact|about|team|support|partners?|press)", low) and urlparse(href).netloc == urlparse(base_url).netloc:
            pages.add(href.split("#")[0])
    clean = {e.strip(". ").lower() for e in emails if e and "@" in e and not _BAD_EMAIL.search(e)}
    return clean, socials, pages


@tool("find_contacts", "Find public contact info for a company/creator website: emails (also Cloudflare-hidden ones), "
      "social profiles and contact pages. Use for outreach, leads, buyers, partners or recruiters.", {
    "url": P("string", "Website or any page on it, e.g. https://example.com"),
}, required=["url"], group="web")
def find_contacts(url: str):
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}/"
    emails: set[str] = set()
    socials: dict[str, str] = {}
    visited, contact_pages = [], set()
    queue = [url] + [root + p for p in CONTACT_PATHS]
    for target in queue:
        if target in visited or len(visited) >= 8:
            continue
        visited.append(target)
        try:
            resp = requests.get(target, headers=HEADERS, timeout=15)
        except requests.RequestException:
            continue
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
            continue
        found, soc, pages = contacts_from_html(resp.text, resp.url)
        emails |= found
        for k, v in soc.items():
            socials.setdefault(k, v)
        contact_pages |= {p for p in pages if p not in visited}
        if target != url and found:
            contact_pages.add(resp.url)
    domain = parsed.netloc.replace("www.", "")
    ranked = sorted(emails, key=lambda e: (not e.endswith(domain), not re.match(r"(hello|hi|contact|partner|business|sales|info|team|founder|press)", e), e))
    return {"site": root, "emails": ranked[:15], "socials": socials, "contact_pages": sorted(contact_pages)[:8],
            "checked": len(visited), "tip": "No email? Use the contact form page or a social profile (DM)." if not ranked else ""}
