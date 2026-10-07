"""What people say about a stock, crypto, forex pair, index or any market topic, and a crawler for any website.

market_sentiment reads public posts from several places at once:
- StockTwits, where traders label their own posts Bullish or Bearish
- the market's subreddits on Reddit
- X, searched in Karya's browser, where the user is logged in
- TradingView ideas (long or short)
- investor forums: ValuePickr for India, Hacker News for tech
- YouTube video titles, any sites the user names, and the news

Each post is scored bullish, bearish or neutral. It also pulls out the price levels traders mention (targets, stops,
support, resistance) and keeps real quotes with links. It reports opinions, never advice.

crawl_site reads one website page by page, politely and following robots.txt, and returns the parts about a topic."""
from __future__ import annotations

import html as htmllib
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from ..registry import P, tool
from .web import HEADERS, html_to_text

REDDIT_HEADERS = {"User-Agent": "windows:karya-personal-agent:1.0 (reads public posts for its user)"}
SOURCES = ("stocktwits", "reddit", "x", "tradingview", "forums", "youtube", "news", "linkedin")
DEFAULT_SOURCES = SOURCES[:-1]     # LinkedIn when asked: it's slower and reads with the user's account
BUDGET = 40          # seconds for all sources together; a slow one is reported, never waited for
CACHE_SECONDS = 600


class SourceError(RuntimeError):
    """A source that couldn't be read this time (rate limit, login, site change), with a reason for the user."""


# ---------------------------------------------------------------- HTTP, with a short cache (Reddit rate-limits fast)
_CACHE: dict[str, tuple[float, requests.Response]] = {}
_CACHE_LOCK = threading.Lock()


def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 15) -> requests.Response:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    resp = requests.get(url, params=params, headers=headers or HEADERS, timeout=timeout)
    if resp.ok:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), resp)
            if len(_CACHE) > 300:
                for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
                    _CACHE.pop(old, None)
    return resp


def _check(resp: requests.Response, site: str) -> requests.Response:
    if resp.status_code == 429:
        raise SourceError(f"{site} is rate-limiting requests right now; try again in a minute")
    if resp.status_code in (401, 403):
        raise SourceError(f"{site} refused the request ({resp.status_code})")
    if resp.status_code >= 400:
        raise SourceError(f"{site} answered {resp.status_code}")
    return resp


# ---------------------------------------------------------------- times
def _parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    found = re.search(r"(\d+)\s*(sec|min|hr|hour|day|week|month|year)", text, re.I)
    if not found and len(text) <= 12:          # LinkedIn's short forms: 45m, 3h, 2d, 1w, 5mo, 1yr
        short = re.match(r"^(\d+)\s*(mo|yr|y|w|d|h|m|s)\b", text, re.I)
        if short:
            n = int(short.group(1))
            seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000,
                       "yr": 31536000}[short.group(2).lower()]
            return datetime.now(timezone.utc) - timedelta(seconds=n * seconds)
    if found:
        n, unit = int(found.group(1)), found.group(2).lower()
        seconds = {"sec": 1, "min": 60, "hr": 3600, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000,
                   "year": 31536000}[unit]
        return datetime.now(timezone.utc) - timedelta(seconds=n * seconds)
    return None


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 14 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return dt.strftime("%Y-%m-%d")


def _plain(markup: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(htmllib.unescape(markup or ""), "html.parser").get_text(" ")).strip()


# ---------------------------------------------------------------- which asset the user means
_FILLER = re.compile(r"\b(stocks?|shares?|share price|price|today|now|sentiment|opinions?|views?|buzz|chatter|"
                     r"what (do |are )?people (think|say|saying)( about| of)?|traders?|investors?|outlook|analysis|"
                     r"news|crypto|coin|token|forex|ltd|limited|inc|the)\b", re.I)
_GENERIC_NAME = re.compile(r"\b(ltd|limited|inc|corp|corporation|co|company|industries|holdings?|group|plc|"
                           r"technologies|solutions|services|enterprises|international|the|of|and)\b\.?", re.I)
ALIASES = {
    "gold": dict(name="Gold (XAUUSD)", stocktwits="XAUUSD", tradingview="XAUUSD", yahoo="GC=F", market="fx",
                 terms=["gold", "xauusd", "xau"]),
    "silver": dict(name="Silver (XAGUSD)", stocktwits="XAGUSD", tradingview="XAGUSD", yahoo="SI=F", market="fx",
                   terms=["silver", "xagusd"]),
    "bitcoin": dict(name="Bitcoin", stocktwits="BTC.X", tradingview="BTCUSD", yahoo="BTC-USD", market="crypto",
                    terms=["bitcoin", "btc"], cashtag="BTC"),
    "ethereum": dict(name="Ethereum", stocktwits="ETH.X", tradingview="ETHUSD", yahoo="ETH-USD", market="crypto",
                     terms=["ethereum", "eth"], cashtag="ETH"),
    "nifty": dict(name="Nifty 50", stocktwits="NIFTY50.NSE", tradingview="NSE-NIFTY", yahoo="^NSEI", market="india",
                  terms=["nifty"]),
    "bank nifty": dict(name="Bank Nifty", stocktwits="NIFTYBANK.NSE", tradingview="NSE-BANKNIFTY", yahoo="^NSEBANK",
                       market="india", terms=["bank nifty", "banknifty"]),
    "sensex": dict(name="Sensex", tradingview="BSE-SENSEX", yahoo="^BSESN", market="india", terms=["sensex"]),
    "s&p 500": dict(name="S&P 500", stocktwits="SPY", tradingview="SPX", yahoo="^GSPC", market="us",
                    terms=["s&p", "spx", "spy", "s&p 500", "sp500"], cashtag="SPY"),
    "nasdaq": dict(name="Nasdaq 100", stocktwits="QQQ", tradingview="NDX", yahoo="^NDX", market="us",
                   terms=["nasdaq", "qqq", "ndx"], cashtag="QQQ"),
    "crude oil": dict(name="Crude oil", stocktwits="USO", tradingview="USOIL", yahoo="CL=F", market="fx",
                      terms=["crude", "oil", "wti", "brent", "usoil"]),
}
for _alias, _key in (("xauusd", "gold"), ("xagusd", "silver"), ("btc", "bitcoin"), ("eth", "ethereum"),
                     ("nifty 50", "nifty"), ("nifty50", "nifty"), ("banknifty", "bank nifty"), ("spx", "s&p 500"),
                     ("sp500", "s&p 500"), ("s&p", "s&p 500"), ("spy", "s&p 500"), ("qqq", "nasdaq"),
                     ("nasdaq 100", "nasdaq"), ("crude", "crude oil"), ("oil", "crude oil"), ("wti", "crude oil")):
    ALIASES[_alias] = ALIASES[_key]


def _core_name(title: str, base: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9&]+", _GENERIC_NAME.sub(" ", title or "")) if len(w) > 1]
    return " ".join(words[:2]) or base


def _full_name(title: str) -> str:
    """'Reliance Industries Ltd.' -> 'Reliance Industries' (only the legal suffix goes)."""
    name = re.sub(r"\b(ltd|limited|inc|corp|corporation|plc|co|llc|pvt|private)\b\.?", " ", title or "", flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" .,&-")


def _stocktwits_lookup(text: str) -> dict | None:
    try:
        resp = _get("https://api.stocktwits.com/api/2/search/symbols.json", params={"q": text}, timeout=10)
        results = resp.json().get("results") or [] if resp.ok else []
    except (requests.RequestException, ValueError):
        return None
    want = re.sub(r"[^a-z0-9]", "", text.lower())
    words = [w for w in re.findall(r"[a-z0-9&]+", text.lower()) if len(w) > 1]
    for r in results[:6]:
        symbol, title = str(r.get("symbol") or ""), str(r.get("title") or "")
        base = symbol.split(".")[0].lower()
        if base == want or (words and all(w in title.lower() for w in words)):
            return r
    return None


def resolve(query: str, symbol: str = "") -> dict:
    """The asset (or topic) to look up: names, symbols on each site, the words its posts use, and its market."""
    raw = (symbol or query or "").strip()
    key = re.sub(r"\s+", " ", _FILLER.sub(" ", raw.lower())).strip(" ?.!,") or raw.lower()
    if key in ALIASES:
        return dict(ALIASES[key], query=raw, asset=True)
    found = _stocktwits_lookup(key) if key else None
    if found:
        symbol = str(found["symbol"])
        base, _, suffix = symbol.partition(".")
        exchange = str(found.get("exchange") or "").upper()
        title = str(found.get("title") or symbol)
        core = _core_name(title, base)
        if suffix in ("NSE", "BSE") or exchange in ("NSE", "BSE"):
            market = "india"
            yahoo = base + (".BO" if suffix == "BSE" or exchange == "BSE" else ".NS")
            tradingview = ("BSE-" if suffix == "BSE" else "NSE-") + base
        elif suffix == "X" or exchange == "CRYPTO":
            market, yahoo, tradingview = "crypto", base + "-USD", base + "USD"
        elif exchange == "FX":
            market, yahoo, tradingview = "fx", f"{base}=X", base
        else:
            market, yahoo, tradingview = "us", base, base
        terms = list(dict.fromkeys([base.lower(), core.lower()]))
        return {"name": title, "core": core, "full": _full_name(title), "query": raw, "stocktwits": symbol,
                "tradingview": tradingview, "yahoo": yahoo, "market": market,
                "terms": [t for t in terms if len(t) > 1], "cashtag": base, "asset": True}
    words = [w for w in re.findall(r"[a-z0-9&$#]+", raw.lower()) if len(w) > 2 and w not in _STOP]
    india = bool(re.search(r"\b(nifty|sensex|nse|bse|india|indian|dalal|rupee|sebi|rbi)\b", raw, re.I))
    return {"name": raw, "core": raw, "query": raw, "market": "india" if india else "general",
            "terms": words or [raw.lower()], "asset": False}


# ---------------------------------------------------------------- reading each source
def _post(source, text, url="", author="", when=None, likes=0, label=None, kind="post", replies=0, title="") -> dict:
    return {"source": source, "text": re.sub(r"\s+", " ", str(text or "")).strip(), "url": url, "author": author,
            "time": _parse_time(when) if not isinstance(when, datetime) else when, "likes": int(likes or 0),
            "replies": int(replies or 0), "label": label, "kind": kind, "title": title}


def src_stocktwits(asset: dict, days: int) -> list[dict]:
    symbol = asset.get("stocktwits")
    if not symbol:
        return []
    resp = _check(_get(f"https://api.stocktwits.com/api/2/streams/symbol/{quote(symbol)}.json"), "StockTwits")
    out = []
    for m in resp.json().get("messages") or []:
        user = (m.get("user") or {}).get("username") or ""
        mood = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        out.append(_post("stocktwits", m.get("body"), f"https://stocktwits.com/{user}/message/{m.get('id')}",
                         "@" + user if user else "", m.get("created_at"), (m.get("likes") or {}).get("total"),
                         {"Bullish": "bullish", "Bearish": "bearish"}.get(mood)))
    return out


SUBREDDITS = {
    "india": "IndianStockMarket+IndiaInvestments+IndianStreetBets+DalalStreetTalks+StockMarketIndia+NSEbets",
    "us": "stocks+wallstreetbets+investing+StockMarket+options+ValueInvesting+SecurityAnalysis",
    "crypto": "CryptoCurrency+Bitcoin+CryptoMarkets+ethtrader+btc",
    "fx": "Forex+Gold+Silverbugs+Daytrading+Trading+commodities",
    "general": "stocks+StockMarket+investing+wallstreetbets+IndianStockMarket+IndiaInvestments+Economics",
}


def _search_words(asset: dict) -> str:
    if not asset.get("asset"):
        return asset["query"]
    base, core = asset.get("cashtag") or "", asset.get("core") or asset["name"]
    if asset["market"] in ("us", "crypto") and base:
        return f"{base} OR \"{core}\""
    return f"\"{core}\""


_REDDIT_LOCK = threading.Lock()
_REDDIT_LAST = [0.0]


def _reddit_get(url: str, params: dict) -> requests.Response:
    """Reddit allows few anonymous requests: keep them 2.5 s apart, and after a rate limit wait and try once more."""
    with _REDDIT_LOCK:
        gap = time.time() - _REDDIT_LAST[0]
        if gap < 2.5:
            time.sleep(2.5 - gap)
        resp = _get(url, params=params, headers=REDDIT_HEADERS)
        _REDDIT_LAST[0] = time.time()
        if resp.status_code == 429:
            try:
                pause = float(resp.headers.get("retry-after") or 6)
            except ValueError:
                pause = 6.0
            time.sleep(min(max(pause, 2.0), 3.5))
            resp = _get(url, params=params, headers=REDDIT_HEADERS)
            _REDDIT_LAST[0] = time.time()
    return resp


def src_reddit(asset: dict, days: int) -> list[dict]:
    t = "day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31 else "year"
    subs = SUBREDDITS.get(asset["market"], SUBREDDITS["general"])
    resp = _check(_reddit_get(f"https://www.reddit.com/r/{subs}/search.rss",
                              {"q": _search_words(asset), "restrict_sr": "1", "sort": "new", "t": t}), "Reddit")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SourceError("Reddit sent a page Karya couldn't read") from exc
    out = []
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", "", ns) or "").strip()
        body = _plain(e.findtext("a:content", "", ns) or "")
        body = re.split(r"\s*submitted by\s+/u/", body)[0].strip()
        link = e.find("a:link", ns)
        author = (e.findtext("a:author/a:name", "", ns) or "").replace("/u/", "u/")
        sub = (e.find("a:category", ns).get("term") if e.find("a:category", ns) is not None else "")
        out.append(_post("reddit", f"{title}. {body}" if body else title, link.get("href") if link is not None else "",
                         f"{author} in r/{sub}" if sub else author, e.findtext("a:published", "", ns), title=title))
    return out


def _x_query(asset: dict, days: int) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    if not asset.get("asset"):
        words = asset["query"]
    elif asset["market"] in ("us", "crypto") and asset.get("cashtag"):
        words = f"${asset['cashtag']}"
    elif asset["market"] == "fx":
        words = " OR ".join(dict.fromkeys([asset.get("stocktwits") or "", f"\"{asset['terms'][0]} price\""])).strip(" OR")
    else:
        core = asset.get("core") or asset["name"]
        tag = (asset.get("cashtag") or "").upper() or asset["terms"][0].upper()
        names = [f'"{asset["full"]}"'] if asset.get("full") and asset["full"].lower() != core.lower() else []
        names += [f'"{core} share"', f'"{core} shares"', f'"{core} stock"', f"#{tag}", f"${tag}"]
        words = " OR ".join(dict.fromkeys(names))
    return f"({words}) lang:en since:{since}"


def src_x(asset: dict, days: int) -> list[dict]:
    from . import browser as B
    url = "https://x.com/search?q=" + quote_plus(_x_query(asset, days)) + "&src=typed_query"
    found = B._run("read_feed", url, 2)
    if isinstance(found, str):
        raise SourceError(found.replace("ERROR: ", "")[:200])
    if found.get("login") and not found.get("posts"):
        raise SourceError("not logged in to X in Karya's browser (open x.com there once and log in)")
    return [_post("x", p.get("text"), p.get("url"), p.get("author"), p.get("time"),
                  p.get("likes"), replies=p.get("replies")) for p in found.get("posts") or [] if not p.get("ad")]


def src_linkedin(asset: dict, days: int) -> list[dict]:
    """Posts from LinkedIn's content search, newest first, read in Karya's browser (needs the user's LinkedIn login)."""
    from . import browser as B
    words = asset.get("core") or asset["query"]
    if asset.get("asset") and asset["market"] != "fx":
        words += " share" if asset["market"] == "india" else " stock"
    url = ("https://www.linkedin.com/search/results/content/?keywords=" + quote_plus(words) +
           "&sortBy=%22date_posted%22")
    found = B._run("read_feed", url, 2)
    if isinstance(found, str):
        raise SourceError(found.replace("ERROR: ", "")[:200])
    if found.get("login") and not found.get("posts"):
        raise SourceError("not logged in to LinkedIn in Karya's browser (open linkedin.com there once and log in)")
    return [_post("linkedin", p.get("text"), p.get("url"), p.get("author"), p.get("time"), p.get("likes"))
            for p in found.get("posts") or [] if not p.get("ad")]


def _find_ideas(node, depth: int = 0):
    if depth > 10:
        return None
    if isinstance(node, dict):
        ideas = node.get("ideas")
        if isinstance(ideas, dict):
            items = (ideas.get("data") or {}).get("items")
            if isinstance(items, list) and items:
                return items
        for value in node.values():
            found = _find_ideas(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_ideas(value, depth + 1)
            if found:
                return found
    return None


def src_tradingview(asset: dict, days: int) -> list[dict]:
    path = asset.get("tradingview")
    if not path:
        return []
    resp = _check(_get(f"https://www.tradingview.com/symbols/{quote(path)}/ideas/"), "TradingView")
    items = None
    for blob in re.findall(r'<script[^>]*type="application/prs\.init-data\+json"[^>]*>(.*?)</script>', resp.text, re.S):
        try:
            items = _find_ideas(json.loads(blob))
        except ValueError:
            continue
        if items:
            break
    out = []
    for it in items or []:
        if it.get("is_education") or it.get("is_script"):
            continue
        direction = (it.get("symbol") or {}).get("direction")
        title = str(it.get("name") or "").strip()
        out.append(_post("tradingview", f"{title}. {it.get('description') or ''}", it.get("chart_url") or "",
                         (it.get("user") or {}).get("username") or "", it.get("created_at") or it.get("date_timestamp"),
                         it.get("likes_count"), {1: "bullish", 2: "bearish"}.get(direction), "idea",
                         it.get("comments_count"), title))
    return out


def src_forums(asset: dict, days: int) -> list[dict]:
    words = asset.get("core") or asset["query"]
    if asset["market"] == "india":
        resp = _check(_get("https://forum.valuepickr.com/search.json", params={"q": f"{words} order:latest"}),
                      "ValuePickr")
        data = resp.json()
        topics = {t.get("id"): t for t in data.get("topics") or []}
        out = []
        for p in data.get("posts") or []:
            topic = topics.get(p.get("topic_id")) or {}
            url = f"https://forum.valuepickr.com/t/{topic.get('slug', 'topic')}/{p.get('topic_id')}/{p.get('post_number', 1)}"
            out.append(_post("valuepickr", f"{topic.get('title', '')}. {_plain(p.get('blurb') or '')}", url,
                             p.get("username") or "", p.get("created_at"), p.get("like_count"), kind="forum",
                             title=topic.get("title", "")))
        return out
    since = int((datetime.now(timezone.utc) - timedelta(days=max(1, days))).timestamp())
    query = asset.get("cashtag") if asset.get("asset") and asset["market"] == "us" else words
    resp = _check(_get("https://hn.algolia.com/api/v1/search_by_date",
                       params={"query": query, "tags": "(story,comment)", "hitsPerPage": 30,
                               "numericFilters": f"created_at_i>{since}"}), "Hacker News")
    out = []
    for h in resp.json().get("hits") or []:
        text = h.get("title") or h.get("story_title") or ""
        if h.get("comment_text"):
            text = f"{text}: {_plain(h['comment_text'])}" if text else _plain(h["comment_text"])
        out.append(_post("hackernews", text, f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                         h.get("author") or "", h.get("created_at"), h.get("points"), kind="forum"))
    return out


def src_youtube(asset: dict, days: int) -> list[dict]:
    core = asset.get("core") or asset["query"]
    if not asset.get("asset"):
        words = asset["query"]
    elif asset["market"] == "india":
        words = f"{core} share"
    elif asset["market"] in ("crypto", "fx"):
        words = f"{core} price"
    else:
        words = f"{asset.get('cashtag') or core} stock"
    resp = _check(_get("https://www.youtube.com/results", params={"search_query": words, "sp": "CAI%3D"}), "YouTube")
    found = re.search(r"var ytInitialData = (\{.*?\});</script>", resp.text, re.S)
    if not found:
        raise SourceError("YouTube changed its page")
    videos: list[dict] = []

    def walk(node, depth=0):
        if depth > 40 or len(videos) >= 25:
            return
        if isinstance(node, dict):
            if "videoRenderer" in node:
                videos.append(node["videoRenderer"])
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
    walk(json.loads(found.group(1)))
    out = []
    for v in videos:
        title = "".join(r.get("text", "") for r in (v.get("title") or {}).get("runs", []))
        channel = "".join(r.get("text", "") for r in (v.get("ownerText") or {}).get("runs", []))
        views = re.sub(r"[^\d]", "", (v.get("viewCountText") or {}).get("simpleText") or "") or 0
        out.append(_post("youtube", title, f"https://www.youtube.com/watch?v={v.get('videoId')}", channel,
                         (v.get("publishedTimeText") or {}).get("simpleText"), int(views) // 100, kind="video",
                         title=title))
    return out


def src_news(asset: dict, days: int) -> list[dict]:
    india = asset["market"] == "india"
    words = asset.get("core") or asset["query"]
    if asset.get("asset") and asset["market"] != "fx":
        words += " stock" if not india else " share"
    params = {"q": f"{words} when:{max(1, days)}d", "hl": "en-IN" if india else "en-US", "gl": "IN" if india else "US",
              "ceid": "IN:en" if india else "US:en"}
    resp = _check(_get("https://news.google.com/rss/search", params=params), "Google News")
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise SourceError("Google News sent a page Karya couldn't read") from exc
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append(_post("news", title, item.findtext("link") or "", source, item.findtext("pubDate"), kind="news",
                         title=title))
    return out[:30]


def src_sites(asset: dict, days: int, sites: list[str]) -> list[dict]:
    """Pages on the sites the user named that talk about the asset (a site-restricted search, then the pages)."""
    from .web import web_search
    out = []
    limit = "d" if days <= 1 else "w" if days <= 7 else "m" if days <= 31 else "y"
    for site in sites[:4]:
        host = urlparse(site if "//" in site else "https://" + site).netloc or site
        rows = web_search(f"site:{host} {asset.get('core') or asset['query']}", max_results=4, timelimit=limit)
        for row in (rows if isinstance(rows, list) else [])[:3]:
            try:
                resp = _get(row["url"], timeout=12)
            except requests.RequestException:
                continue
            if not resp.ok or "html" not in resp.headers.get("content-type", "html"):
                continue
            _, text, _ = _page_text(resp.text, resp.url)
            for chunk in _snippets(text, asset["terms"], 4, 400):
                out.append(_post(host, chunk, resp.url, host, None, kind="page", title=row.get("title") or ""))
    return out


# ---------------------------------------------------------------- reading the mood of a post
_W = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
BULLISH = [(re.compile(p, re.I), w) for p, w in (
    (_W + r"bull(ish|s)?" + _E, 2), (_W + r"(going |go )?long(?![- ]?term)" + _E, 1), (_W + r"break ?out(s)?" + _E, 2),
    (_W + r"buy(ing)? the dip|btd" + _E, 2), (_W + r"moon(ing)?|to the moon" + _E, 2), ("🚀|📈|🐂|💹|🟢", 1),
    (_W + r"(all[- ]time high|ath|new high)s?" + _E, 1), (_W + r"rall(y|ying|ies)" + _E, 1),
    (_W + r"upside|uptrend|higher highs|golden cross" + _E, 1), (_W + r"accumulat(e|ing|ion)" + _E, 2),
    (_W + r"add(ing|ed)? more|loaded up|load(ing)? up" + _E, 1), (_W + r"undervalued|cheap" + _E, 1),
    (_W + r"(strong )?buy(ing)?|bought" + _E, 1), (_W + r"calls" + _E, 1), (_W + r"outperform|upgrade[sd]?" + _E, 1),
    (_W + r"beats?|strong results|record (profit|revenue)" + _E, 1), (_W + r"bounce|rebound|recover(y|ing)" + _E, 1),
    (_W + r"(support|demand) (is )?holding|bottomed" + _E, 1), (_W + r"tezi|uchaal" + _E, 2),
    ("तेजी|उछाल|रौनक|खरीद", 2),
)]
BEARISH = [(re.compile(p, re.I), w) for p, w in (
    (_W + r"bear(ish|s)?" + _E, 2), (_W + r"(going |go )?short(ing|ed)?(?![- ]?(term|squeeze))" + _E, 1),
    (_W + r"break ?down|breaks down" + _E, 2), (_W + r"crash(ing|ed)?|dump(ing|ed)?|tank(ing|ed)?" + _E, 2),
    ("📉|🐻|💀|🔴", 1), (_W + r"puts" + _E, 1), (_W + r"overvalued|overpriced|bubble" + _E, 1),
    (_W + r"downside|downtrend|lower lows|death cross" + _E, 1), (_W + r"sell(ing|off|-off)?|sold" + _E, 1),
    (_W + r"avoid|stay away|exit(ed)?" + _E, 1), (_W + r"(sl|stop ?loss) hit" + _E, 1), (_W + r"weak(ness)?" + _E, 1),
    (_W + r"downgrade[sd]?|underperform" + _E, 1), (_W + r"miss(es|ed)?" + _E, 1),
    (_W + r"fall(ing|s)?|drop(ping|s|ped)?|plunge[sd]?|slump(s|ed)?|bleed(ing)?" + _E, 1),
    (_W + r"rejected|rejection" + _E, 1), (_W + r"girawat|mandi" + _E, 2), ("गिरावट|मंदी|बिकवाली|टूटा|टूट", 2),
)]
_NEGATE = re.compile(r"(?:not|no|never|don'?t|dont|isn'?t|aren'?t|won'?t|wont|without|nothing)\W+(?:\w+\W+){0,2}$", re.I)


def mood_of(text: str) -> tuple[str, int]:
    """("bullish" | "bearish" | "neutral", score) from the words and emoji people use."""
    score = 0
    for patterns, sign in ((BULLISH, 1), (BEARISH, -1)):
        for rx, weight in patterns:
            for m in rx.finditer(text or ""):
                negated = bool(_NEGATE.search(text[max(0, m.start() - 30): m.start()]))
                score += -sign * weight if negated else sign * weight
    return ("bullish" if score >= 2 else "bearish" if score <= -2 else "neutral"), score


# ---------------------------------------------------------------- price levels traders mention
_NUM = r"(?:rs\.?|₹|\$|inr|usd)?\s*([\d][\d,]*(?:\.\d+)?)(?:\s?(k)(?![a-z]))?"
LEVELS = {
    "targets": re.compile(r"\b(?:price )?(?:target|tgt|tp\d?|t[123]|pt)s?\b\s*(?:is|of|at|:|-|=|@|→|->)?\s*" + _NUM, re.I),
    "stops": re.compile(r"\b(?:sl|stop[ -]?loss|stoploss|stop)\b\s*(?:is|at|:|-|=|@|→|->)?\s*" + _NUM, re.I),
    "support": re.compile(r"\bsupports?\b\s*(?:is|at|near|around|of|zone|level|:|-|=|@)?\s*" + _NUM, re.I),
    "resistance": re.compile(r"\b(?:resistance|breakout above|breaks? above)\b\s*(?:is|at|near|around|of|zone|"
                             r"level|:|-|=|@)?\s*" + _NUM, re.I),
}


def levels_in(posts: list[dict], price: float | None) -> dict:
    found: dict[str, list[float]] = {k: [] for k in LEVELS}
    for post in posts:
        for kind, rx in LEVELS.items():
            for m in rx.finditer(post["text"]):
                try:
                    value = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
                except ValueError:
                    continue
                if value <= 0:
                    continue
                if 1990 <= value <= 2035 and value == int(value) and not (price and 0.97 * price <= value <= 1.03 * price):
                    continue          # "target 2027" is a year, unless the price itself is right there
                if price and not 0.5 * price <= value <= 1.7 * price:
                    continue
                found[kind].append(value)
    out = {}
    for kind, values in found.items():
        if not values:
            continue
        step = (price or sorted(values)[len(values) // 2]) * 0.005 or 1
        groups: dict[int, list[float]] = {}
        for v in values:
            groups.setdefault(round(v / step), []).append(v)
        top = sorted(groups.values(), key=lambda g: -len(g))[:3]
        out[kind] = [{"level": _round_level(sum(g) / len(g)), "mentions": len(g)} for g in top]
    return out


def _round_level(value: float) -> float:
    return round(value, 2) if value < 10 else round(value, 1) if value < 1000 else round(value)


# ---------------------------------------------------------------- what people talk about
_STOP = set("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our out
over own same she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your yours im ive dont cant
going get got like one see think know really still even much way well make made want need good time day days today
week year let lol yes yeah ok also via amp http https www com new people stock stocks share shares market price
buy sell long short bullish bearish""".split())


# Words every market post uses: they say nothing about what this crowd is discussing.
_MARKET_WORDS = set("""chart charts move moves moving key back high highs low lows next latest level levels trade trades
trading trader traders structure prediction predictions analysis update updates today tomorrow yesterday week weekly
daily month monthly year years may might could would should will price prices target targets stock stocks share
shares market markets nse bse india indian sensex nifty50 investing invest investor investors money video watch live
news view views big best top right left old way thing things lot lots look looks looking seen point points zone zones
setup setups idea ideas usd inr crore lakh percent support resistance breakout breakdown bullish bearish buy sell
buying selling long short entry exit stop loss profit going gonna right now call calls puts put options option
dip run rally pump dump red green near around above below hold holding think first last since even""".split())


def themes(posts: list[dict], asset: dict) -> list[str]:
    own = " ".join(str(asset.get(k) or "") for k in ("name", "core", "stocktwits", "tradingview", "yahoo", "cashtag"))
    skip = set(_STOP) | _MARKET_WORDS | {w for w in re.findall(r"[a-z0-9]+", own.lower())} | {
        t for term in asset["terms"] for t in term.split()}
    unigrams, bigrams = Counter(), Counter()
    by_author: dict[str, tuple[set, set]] = {}
    for post in posts:
        words = re.findall(r"[a-z][a-z0-9'&-]{2,}", post["text"].lower())
        uni, bi = by_author.setdefault((post.get("author") or post.get("url") or "").lower(), (set(), set()))
        uni.update(w for w in words if w not in skip)
        bi.update(f"{a} {b}" for a, b in zip(words, words[1:]) if a not in skip and b not in skip and a != b)
    for uni, bi in by_author.values():     # how many different people say it, not how often one account does
        unigrams.update(uni)
        bigrams.update(bi)
    scored = [(n * 1.6, g) for g, n in bigrams.items() if n >= 2] + [(float(n), g) for g, n in unigrams.items() if n >= 3]
    picked: list[str] = []
    for _, gram in sorted(scored, key=lambda x: -x[0]):
        if len(picked) >= 8:
            break
        if any(gram in p or p in gram for p in picked):
            continue
        picked.append(gram)
    return picked


def also_mentioned(posts: list[dict], asset: dict) -> list[str]:
    own = (asset.get("cashtag") or "").upper()
    tags = Counter(t.upper() for post in posts for t in set(re.findall(r"\$([A-Za-z]{1,6}(?:\.[A-Z]{1,4})?)\b",
                                                                        post["text"])))
    return [f"${t} ({n})" for t, n in tags.most_common(8) if t.split(".")[0] != own and n >= 2][:5]


# ---------------------------------------------------------------- putting it together
_MARKET_CONTEXT = re.compile(r"(?<![a-z])(shares?|stocks?|nse|bse|nifty|sensex|ipo|targets?|stop ?loss|buy(ing)?|"
                             r"sell(ing)?|bull(ish)?|bear(ish)?|q[1-4]|fy\d\d|results|earnings|dividends?|valuation|"
                             r"portfolio|invest\w*|trad(e|es|ing|ers?)|charts?|support|resistance|breakout|price|rs\.?|"
                             r"crypto|forex|futures|options|calls|puts|rally|crash|market ?cap|demerger|listing|merger|"
                             r"acquisition|acquir\w+|stake|deal|quarter\w*|revenue|profits?|ceo|chairman|shareholders?|"
                             r"company|conglomerate|subsidiary|business)(?![a-z])|₹", re.I)


_STOCK_TALK = re.compile(r"(?<![a-z])(shares?|stocks?|nse|bse|nifty|sensex|ipo|targets?|stop ?loss|bull(ish)?|"
                         r"bear(ish)?|q[1-4] results|results|earnings|dividends?|valuation|invest(ors?|ing|ment)?|"
                         r"trad(e|ing|ers?)|share price|market ?cap|demerger|listing|merger|acquisition|stake|"
                         r"buy(ing)? the|sell(ing)? the|rally|crash|price target)(?![a-z])|₹", re.I)


def _relevant(post: dict, asset: dict) -> bool:
    if post["source"] in ("stocktwits", "tradingview"):
        return True                         # asked by symbol
    text = f"{post.get('title', '')} {post['text']}".lower()
    hits = [t for t in asset["terms"] if re.search(r"(?<![a-z0-9-])[$#]?" + re.escape(t) + r"(?![a-z0-9])", text)]
    if not asset.get("asset"):
        return len(hits) >= max(1, (len(asset["terms"]) + 1) // 2)
    if not hits:
        return False
    if post["source"] == "linkedin":
        # people name their employer all the time ("my internship at Reliance Industries"): stock talk only
        return bool(_STOCK_TALK.search(text))
    if post["source"] == "x":
        # X and LinkedIn are searched across everything: "reliance on imports" isn't about Reliance's stock
        tag = (asset.get("cashtag") or "").lower()
        if tag and re.search(r"[$#]" + re.escape(tag) + r"(?![a-z0-9])", text):
            return True
        if asset.get("full") and asset["full"].lower() in text:
            return True
        return bool(_MARKET_CONTEXT.search(text))
    return True


def _example(post: dict) -> dict:
    text = post["text"]
    return {k: v for k, v in {"source": post["source"], "who": post.get("author"), "when": _ago(post.get("time")),
                              "said": text[:230] + ("…" if len(text) > 230 else ""), "likes": post.get("likes") or None,
                              "url": post.get("url")}.items() if v not in (None, "")}


def _mood_line(bull: int, bear: int) -> str:
    opinions = bull + bear
    if opinions < 5:
        return f"Not enough opinions to call ({opinions} bullish or bearish posts)"
    share = bull / opinions
    word = ("Mostly bullish" if share >= 0.65 else "Leaning bullish" if share >= 0.55 else
            "Mostly bearish" if share <= 0.35 else "Leaning bearish" if share <= 0.45 else "Mixed")
    return f"{word}: {bull} bullish vs {bear} bearish posts ({round(100 * share)}% bullish)"


_RESULTS: dict[str, tuple[float, dict]] = {}


def run(query: str, symbol: str = "", sources: list[str] | None = None, days: int = 7, sites: list[str] | None = None,
        include_x: bool = True) -> dict:
    days = max(1, min(int(days or 7), 90))
    wanted = [s for s in (sources or DEFAULT_SOURCES) if s in SOURCES]
    if not include_x:
        wanted = [s for s in wanted if s != "x"]
    key = json.dumps([query.lower(), symbol.lower(), sorted(wanted), days, sites or []])
    hit = _RESULTS.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return dict(hit[1], cached=f"from {int((time.time() - hit[0]) // 60)} min ago")
    asset = resolve(query, symbol)
    started = time.time()
    tasks = {name: (globals()[f"src_{name}"], (asset, days)) for name in wanted}
    if sites:
        tasks["sites"] = (src_sites, (asset, days, list(sites)))
    pool = ThreadPoolExecutor(max_workers=len(tasks) + 1)
    futures = {pool.submit(fn, *args): name for name, (fn, args) in tasks.items()}
    price_future = pool.submit(_price, asset) if asset.get("yahoo") else None
    done, _ = wait(list(futures) + ([price_future] if price_future else []), timeout=BUDGET)
    pool.shutdown(wait=False, cancel_futures=True)
    posts, failed, counts = [], {}, {}
    for fut, name in futures.items():
        if fut not in done:
            failed[name] = f"still loading after {BUDGET}s, skipped this time"
            continue
        try:
            rows = fut.result() or []
        except SourceError as exc:
            failed[name] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 - one broken site never breaks the answer
            failed[name] = f"couldn't read it ({type(exc).__name__})"
            continue
        counts[name] = len(rows)
        posts.extend(rows)
    price = price_future.result() if price_future is not None and price_future in done else None
    now = datetime.now(timezone.utc)
    window = {"tradingview": max(days, 30), "youtube": max(days, 7)}
    kept, seen = [], set()
    for post in posts:
        limit = timedelta(days=window.get(post["source"], days))
        if post["time"] is not None and now - post["time"] > limit:
            continue
        sig = post["url"] or post["text"][:120]
        same = re.sub(r"[^a-z]+", " ", post["text"].lower()).strip()[:90]
        if not post["text"] or sig in seen or (len(same) > 30 and same in seen) or not _relevant(post, asset):
            continue
        seen.update({sig, same})
        if not post.get("label"):
            post["label"] = mood_of(post["text"])[0]
        kept.append(post)
    crowd = [p for p in kept if p["kind"] != "news"]
    news = [p for p in kept if p["kind"] == "news"]
    by_source: dict[str, dict] = {}
    for post in kept:
        row = by_source.setdefault(post["source"], {"posts": 0, "bullish": 0, "bearish": 0, "neutral": 0})
        row["posts"] += 1
        row[post["label"]] += 1
    voices: Counter = Counter()
    bull = bear = 0
    for post in crowd:               # one loud account (or a bot) counts at most twice
        who = (post.get("author") or "").lower() or post["url"]
        voices[who] += 1
        if voices[who] > 2:
            continue
        bull += post["label"] == "bullish"
        bear += post["label"] == "bearish"
    rank = lambda p: (p.get("likes") or 0) + 2 * (p.get("replies") or 0)  # noqa: E731
    pick = lambda label: [_example(p) for p in sorted((p for p in crowd if p["label"] == label), key=rank,
                                                     reverse=True)[:4]]
    tag = asset.get("stocktwits")
    out = {"asking_about": asset["name"] + (f" ({tag})" if tag and tag.lower() not in asset["name"].lower() else ""),
           "window": f"last {days} day{'s' if days != 1 else ''}",
           "read": f"{len(kept)} relevant posts in {round(time.time() - started)}s",
           "crowd_mood": _mood_line(bull, bear), "by_source": by_source}
    if price:
        out["price_now"] = price
    levels = levels_in(crowd, (price or {}).get("price"))
    if levels:
        out["levels_traders_mention"] = levels
    talk = themes(crowd, asset)
    if talk:
        out["talking_about"] = talk
    others = also_mentioned(crowd, asset)
    if others:
        out["also_mentioned"] = others
    out["bullish_examples"] = pick("bullish")
    out["bearish_examples"] = pick("bearish")
    latest = sorted((p for p in crowd if p["time"]), key=lambda p: p["time"], reverse=True)[:4]
    out["latest"] = [_example(p) for p in latest]
    if news:
        tone = Counter(p["label"] for p in news)
        out["news"] = {"headlines": [_example(p) for p in sorted(news, key=lambda p: p["time"] or now,
                                                                 reverse=True)[:5]],
                       "tone": f"{tone['bullish']} positive, {tone['bearish']} negative, {tone['neutral']} neutral"}
    if failed:
        out["not_read"] = failed
    if not kept:
        out["more"] = ("No posts matched. Try the company's full name or its ticker (e.g. RELIANCE, NVDA, BTC), more "
                       "days, or name sites to read with sites=[...].")
    out["note"] = ("What people are posting, not advice: social posts can be hype, bots or paid promotion, and the "
                   "mood is counted from their words. StockTwits and TradingView labels are the traders' own.")
    while len(json.dumps(out, ensure_ascii=False, default=str)) > 9_500:
        for field in ("latest", "bearish_examples", "bullish_examples"):
            if len(out.get(field) or []) > 2:
                out[field].pop()
                break
        else:
            break
    _RESULTS[key] = (time.time(), out)
    return out


def _price(asset: dict) -> dict | None:
    from .finance import quote as stock_price
    try:
        q = stock_price(asset["yahoo"])
    except Exception:  # noqa: BLE001 - the price is extra context
        return None
    if not q.get("price"):
        return None
    return {k: q.get(k) for k in ("symbol", "price", "change_pct", "currency", "as_of") if q.get(k) is not None}


@tool("market_sentiment", "What people are saying right now about a stock, crypto, forex pair, index or any market "
      "topic (e.g. 'Reliance', 'NVDA', 'bitcoin', 'gold', 'Nifty', 'Fed rate cut'). Reads StockTwits (traders' own "
      "bullish/bearish labels), Reddit (the market's subreddits), X (search in Karya's browser, where the user is "
      "logged in), TradingView ideas (long/short), investor forums (ValuePickr for India, Hacker News), YouTube video "
      "titles and the news, all at once. Returns the crowd mood with counts per source, the price levels traders "
      "mention (targets, stops, support, resistance), what they talk about, and real quotes with links.", {
    "query": P("string", "Company, ticker, coin, pair, index or topic, e.g. 'Tata Motors', 'AAPL', 'XAUUSD', 'Nifty'"),
    "symbol": P("string", "Optional exact symbol if you know it (e.g. RELIANCE.NSE, NVDA, BTC.X)"),
    "sources": P("array", "Subset of: " + ", ".join(SOURCES) + " (default all but linkedin; add 'linkedin' for "
                          "LinkedIn posts, read in Karya's browser)", items={"type": "string"}),
    "days": P("integer", "How far back, in days (default 7)"),
    "sites": P("array", "Other websites to search for posts about it, e.g. ['moneycontrol.com', 'forum.example.com']",
               items={"type": "string"}),
    "include_x": P("boolean", "Read X in Karya's browser (default true; needs the user's X login there)"),
}, required=["query"], group="finance")
def market_sentiment(query: str, symbol: str = "", sources: list[str] | None = None, days: int = 7,
                     sites: list[str] | None = None, include_x: bool = True):
    return run(query, symbol, sources, days, sites, include_x)


# ---------------------------------------------------------------- crawl any website
_SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|css|js|mjs|json|xml|rss|atom|pdf|zip|gz|rar|7z|exe|msi|dmg|"
                       r"mp[34]|m4a|wav|avi|mov|webm|woff2?|ttf|eot|csv|xlsx?|docx?|pptx?)$", re.I)
_TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|ref|ref_src|mc_[a-z]+|igshid)$", re.I)


def _norm_url(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    query = "&".join(p for p in parsed.query.split("&") if p and not _TRACKING.match(p.split("=")[0]))
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return f"{parsed.scheme}://{parsed.netloc.lower()}{path}" + (f"?{query}" if query else "")


def _page_text(markup: str, url: str) -> tuple[str, str, list[dict]]:
    """Title, text and links of a page. Forums like Discourse (ValuePickr) put the posts inside <noscript> for
    readers without JavaScript; that copy is used when the normal text is empty. Links are every link on the page
    (short ones like "TCS" or "2", and pagination outside the main content, count for a crawl)."""
    title, text, _ = html_to_text(markup, url)
    bare = re.sub(r"</?noscript[^>]*>", "", markup, flags=re.I) if "<noscript" in markup.lower() else markup
    if len(text) < 200 and bare is not markup:
        title2, text2, _ = html_to_text(bare, url)
        if len(text2) > len(text):
            title, text = title2 or title, text2
    links, seen = [], set()
    for a in BeautifulSoup(bare, "html.parser").find_all("a", href=True):
        href = urljoin(url, a["href"].strip())
        if href.startswith("http") and href not in seen:
            seen.add(href)
            links.append({"url": href, "text": a.get_text(" ", strip=True)[:80]})
            if len(links) >= 600:
                break
    return title, text, links


def _snippets(text: str, words: list[str], limit: int = 3, size: int = 300) -> list[str]:
    if not words:
        return []
    rx = re.compile("|".join(r"(?<![a-z0-9])" + re.escape(w.lower()) for w in words if w), re.I)
    out = []
    for block in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z0-9])", text or ""):
        block = block.strip()
        if len(block) >= 25 and rx.search(block):
            out.append(block[:size] + ("…" if len(block) > size else ""))
            if len(out) >= limit:
                break
    return out


def _robots(root: str) -> tuple[RobotFileParser, list[str], float]:
    rp = RobotFileParser()
    sitemaps: list[str] = []
    delay = 0.0
    try:
        resp = requests.get(root + "/robots.txt", headers=HEADERS, timeout=10)
        lines = resp.text.splitlines() if resp.ok and "html" not in resp.headers.get("content-type", "") else []
    except requests.RequestException:
        lines = []
    rp.parse(lines)
    sitemaps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]
    try:
        delay = float(rp.crawl_delay(HEADERS["User-Agent"]) or rp.crawl_delay("*") or 0)
    except (TypeError, ValueError):
        delay = 0.0
    return rp, sitemaps, delay


def _sitemap_urls(sitemaps: list[str], host: str, cap: int = 400, seconds: float = 15) -> list[str]:
    urls: list[str] = []
    queue = list(sitemaps[:3])
    seen = set()
    deadline = time.time() + seconds
    bare = host[4:] if host.startswith("www.") else host
    while queue and len(urls) < cap and len(seen) < 6 and time.time() < deadline:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            resp = requests.get(sm, headers=HEADERS, timeout=min(10, max(1, deadline - time.time())))
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text) if resp.ok else []
        except requests.RequestException:
            continue
        for loc in locs:
            loc = htmllib.unescape(loc)
            loc_host = urlparse(loc).netloc.lower()
            if loc.lower().endswith(".xml") or "sitemap" in loc.lower().rsplit("/", 1)[-1]:
                queue.append(loc)
            elif (loc_host[4:] if loc_host.startswith("www.") else loc_host) == bare:
                urls.append(loc)
    return urls[:cap]


# Sites that show their content only to a logged-in person (or only with JavaScript): read in Karya's browser,
# where the user is logged in, at a reading pace. Everything else is read over plain HTTP, following robots.txt.
BROWSER_SITES = re.compile(r"(^|\.)(linkedin\.com|x\.com|twitter\.com|facebook\.com|instagram\.com|threads\.(net|com)|"
                           r"glassdoor\.[a-z.]+|naukri\.com|wellfound\.com|quora\.com|discord\.com|"
                           r"web\.whatsapp\.com|tiktok\.com|pinterest\.[a-z.]+)$", re.I)
# "crawl linkedin for ..." / "search x for ...": a site's own search page for the words.
SEARCH_PAGES = {
    "linkedin.com": "https://www.linkedin.com/search/results/content/?keywords={q}&sortBy=%22date_posted%22",
    "x.com": "https://x.com/search?q={q}&src=typed_query&f=live",
    "reddit.com": "https://www.reddit.com/search/?q={q}&sort=new",
    "youtube.com": "https://www.youtube.com/results?search_query={q}",
    "facebook.com": "https://www.facebook.com/search/posts/?q={q}",
    "instagram.com": "https://www.instagram.com/explore/search/keyword/?q={q}",
    "github.com": "https://github.com/search?q={q}&type=repositories",
    "quora.com": "https://www.quora.com/search?q={q}",
    "medium.com": "https://medium.com/search?q={q}",
    "news.ycombinator.com": "https://hn.algolia.com/?q={q}",
}
SITE_NAMES = {"linkedin": "linkedin.com", "x": "x.com", "twitter": "x.com", "twitter.com": "x.com", "reddit": "reddit.com",
              "youtube": "youtube.com", "yt": "youtube.com", "facebook": "facebook.com", "fb": "facebook.com",
              "instagram": "instagram.com", "insta": "instagram.com", "github": "github.com", "quora": "quora.com",
              "medium": "medium.com", "hackernews": "news.ycombinator.com", "hn": "news.ycombinator.com"}
# The page a site sends when it wants a login, a check or a CAPTCHA: Karya stops there and tells the user.
_WALL = re.compile(r"/(login|signin|sign-in|authwall|checkpoint|uas/login|i/flow/login|accounts/login|signup)\b", re.I)


def _start_url(url: str, query: str) -> tuple[str, bool]:
    """(where to start, is it a search page). A bare site name or a site's home page plus words becomes that site's
    own search page for the words."""
    raw = (url or "").strip()
    name = raw.lower().rstrip("/")
    name = SITE_NAMES.get(name.removeprefix("www."), name.removeprefix("www."))
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + (name if "." in name else raw).lstrip("/")
    parsed = urlparse(raw)
    host = SITE_NAMES.get(parsed.netloc.lower().removeprefix("www."), parsed.netloc.lower().removeprefix("www."))
    if query.strip() and (parsed.path in ("", "/")) and not parsed.query and host in SEARCH_PAGES:
        return SEARCH_PAGES[host].format(q=quote_plus(query.strip())), True
    return raw, False


@tool("crawl_site", "Crawl ANY website the user names and return the pages and passages about a topic: forums, blogs, "
      "news, company and docs sites, and logged-in sites like LinkedIn, X, Facebook or Instagram. url can be a page, a "
      "site ('linkedin', 'x', 'reddit', 'moneycontrol.com') or a site's home page plus query (Karya then starts at that "
      "site's own search). Public sites are read over HTTP, following robots.txt; login sites are read in Karya's "
      "browser, where the user is logged in, at a reading pace. It only reads: never clicks, posts, follows or logs in.", {
    "url": P("string", "Page or site to crawl, e.g. https://forum.example.com, 'linkedin', 'x.com', 'moneycontrol.com'"),
    "query": P("string", "Words to look for (leave empty to just map the site)"),
    "max_pages": P("integer", "How many pages to read (default 25; login sites default 12, max 30)"),
    "include_subdomains": P("boolean", "Also follow links to other subdomains of the same site (default false)"),
    "use_browser": P("boolean", "Read in Karya's browser (logged in, runs JavaScript). Default: automatic"),
}, required=["url"], group="web")
def crawl_site(url: str, query: str = "", max_pages: int | None = None, include_subdomains: bool = False,
               use_browser: bool | None = None):
    start_raw, searched = _start_url(url, query)
    start = _norm_url(start_raw)
    parsed = urlparse(start)
    host, root = parsed.netloc.lower(), f"{parsed.scheme}://{parsed.netloc}"
    browser = bool(BROWSER_SITES.search(host)) if use_browser is None else bool(use_browser)
    words = [w for w in re.findall(r"[a-z0-9$#&.+-]{2,}", (query or "").lower()) if w not in _STOP][:8]
    seeds: list[str] = []
    if words and not searched and parsed.path in ("", "/") and not browser:
        from .web import web_search          # a site's home page plus words: start from its pages about them
        rows = web_search(f"site:{host.removeprefix('www.')} {query}", max_results=10)
        seeds = [r["url"] for r in (rows if isinstance(rows, list) else []) if r.get("url")]
    state = {"host": host, "root": root, "domain": ".".join(host.split(".")[-2:]), "words": words,
             "include_subdomains": include_subdomains}
    if browser:
        cap = max(1, min(int(max_pages or 12), 30))
        out = _crawl_browser(start, seeds, cap, state)
    else:
        cap = max(1, min(int(max_pages or 25), 60))
        out = _crawl_http(start, seeds, cap, state)
        if isinstance(out, str) and use_browser is None:
            note = out                       # robots.txt or the site said no to a crawler
            out = _crawl_browser(start, seeds, min(cap, 12), state)
            if isinstance(out, dict):
                if "robots.txt" in note:
                    out["mode"] += (" - robots.txt asks crawlers to stay out, so only the page(s) you asked for were "
                                    "read, and links were followed only where it allows")
                else:
                    out["mode"] += f" ({note.split(': ', 1)[-1]})"
        elif isinstance(out, dict) and out.pop("_mostly_empty", False) and use_browser is None:
            again = _crawl_browser(start, seeds, min(cap, 12), state)
            if isinstance(again, dict) and again.get("pages_read"):
                again["mode"] += " (the plain pages had almost no text: this site builds them with JavaScript)"
                out = again
    if isinstance(out, dict):
        if searched:
            out["started_at"] = start
        while len(json.dumps(out, ensure_ascii=False)) > 9_500 and len(out.get("pages") or []) > 3:
            out["pages"].pop()
    return out


def _same_site(state: dict, link_host: str) -> bool:
    bare = lambda h: h.lower()[4:] if h.lower().startswith("www.") else h.lower()  # noqa: E731
    link_host = link_host.lower()
    if bare(link_host) == bare(state["host"]):
        return True
    return state["include_subdomains"] and (link_host == state["domain"] or link_host.endswith("." + state["domain"]))


def _priority(state: dict, link: str, anchor: str = "") -> int:
    low = (link + " " + anchor).lower()
    return sum(3 for w in state["words"] if w in low)


def _page_row(state: dict, url: str, title: str, text: str, depth: int, posts: list | None = None) -> dict:
    words = state["words"]
    page = {"url": url, "title": (title or "")[:120], "depth": depth}
    if words:
        page["matches"] = sum(len(re.findall(r"(?<![a-z0-9])" + re.escape(w), text.lower())) for w in words)
        page["passages"] = _snippets(text, words, 3, 300)
    else:
        page["summary"] = re.sub(r"\s+", " ", text[:240])
    found = []
    for post in posts or []:
        said = re.sub(r"\s+", " ", str(post.get("text") or "")).strip()
        hits = sum(1 for w in words if w in said.lower())
        if not said or (words and not hits):
            continue
        found.append((hits, {k: v for k, v in {"who": post.get("author"), "when": post.get("time"),
                                               "said": said[:280] + ("…" if len(said) > 280 else ""),
                                               "url": post.get("url")}.items() if v}))
    found = [row for _, row in sorted(found, key=lambda x: -x[0])]
    if found:
        page["posts"] = found[:8]
        page.pop("passages", None)            # the posts say it better than text cut from the page
        page["matches"] = max(page.get("matches", 0), len(found))
    return page


def _queue_links(state: dict, frontier: list, queued: set, links: list[dict], depth: int) -> None:
    social = bool(BROWSER_SITES.search(state["host"]))
    for row in links:
        target = _norm_url(row.get("url") or "")
        lp = urlparse(target)
        if lp.scheme not in ("http", "https") or not _same_site(state, lp.netloc) or _SKIP_EXT.search(lp.path):
            continue
        if target in queued or depth >= 4 or _WALL.search(lp.path):
            continue
        if social and (depth >= 1 or not _SOCIAL_CONTENT.search(lp.path)):
            continue          # on LinkedIn / X: the posts and articles themselves, not menus or other profiles
        queued.add(target)
        frontier.append((-_priority(state, target, row.get("text", "")), depth + 1, target))


_SOCIAL_CONTENT = re.compile(r"/(feed/update|posts?|pulse|jobs/view|status|p|reel|permalink|events/\d|"
                             r"groups/[^/]+/(posts|permalink))/", re.I)


def _result(state: dict, pages: list[dict], frontier: list, mode: str, extra: dict) -> dict:
    if state["words"]:
        found = sorted((p for p in pages if p.get("matches")), key=lambda p: -p["matches"])
        out = {"site": state["host"], "mode": mode, "looked_for": " ".join(state["words"]), "pages_read": len(pages),
               "pages_with_matches": len(found), "pages": found[:15]}
    else:
        out = {"site": state["host"], "mode": mode, "pages_read": len(pages), "pages": pages[:30]}
    if frontier:
        out["not_read_yet"] = f"{len(frontier)} more pages found (raise max_pages to read more)"
    out.update({k: v for k, v in extra.items() if v})
    return out


def _crawl_http(start: str, seeds: list[str], max_pages: int, state: dict):
    rp, sitemaps, delay = _robots(state["root"])
    agent = HEADERS["User-Agent"]
    if not rp.can_fetch(agent, start):
        return f"NOT CRAWLED: {state['host']}'s robots.txt asks crawlers not to read {urlparse(start).path or '/'}"
    frontier: list[tuple[int, int, str]] = [(-100, 0, start)]
    frontier += [(-90 - i, 1, _norm_url(s)) for i, s in enumerate(seeds[:10])]
    for link in _sitemap_urls(sitemaps or [state["root"] + "/sitemap.xml"], state["host"]):
        frontier.append((-_priority(state, link), 1, link))
    queued = {u for _, _, u in frontier}
    pages, errors, blocked, thin, refused = [], [], 0, 0, 0
    started = time.time()
    pause = min(max(delay, 0.5), 5.0)
    workers = 1 if delay >= 1 else 2
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        while frontier and len(pages) < max_pages and time.time() - started < 75:
            frontier.sort()
            batch = []
            while frontier and len(batch) < workers and len(pages) + len(batch) < max_pages:
                _, depth, link = frontier.pop(0)
                if not rp.can_fetch(agent, link):
                    blocked += 1
                    continue
                batch.append((depth, link))
            if not batch:
                break
            for (depth, link), resp in pool.map(lambda item: (item, _safe_fetch(item[1])), batch):
                if isinstance(resp, str):
                    errors.append(f"{link}: {resp}")
                    continue
                if resp.status_code in (401, 403, 429, 999):
                    refused += 1
                    errors.append(f"{link}: HTTP {resp.status_code}")
                    continue
                if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html"):
                    if resp.status_code >= 400:
                        errors.append(f"{link}: HTTP {resp.status_code}")
                    continue
                title, text, links = _page_text(resp.text, resp.url)
                if len(text) < 200:
                    thin += 1
                pages.append(_page_row(state, resp.url, title, text, depth))
                _queue_links(state, frontier, queued, links, depth)
            time.sleep(pause)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not pages and refused:
        return f"NOT CRAWLED: {state['host']} refused plain requests (HTTP {errors[0].rsplit(' ', 1)[-1]})"
    out = _result(state, pages, frontier, "read over HTTP (robots.txt followed)",
                  {"skipped_by_robots_txt": blocked, "errors": errors[:5]})
    if pages and thin >= max(2, len(pages) // 2):
        out["_mostly_empty"] = True
    return out


def _crawl_browser(start: str, seeds: list[str], max_pages: int, state: dict):
    """Pages read in Karya's browser like a person reading: one at a time, ~3 seconds apart, scrolled once so lazy
    feeds load. It never clicks, never logs in and never solves a check: on a login or security page it stops.
    The pages the user asked for are always read. On public sites, links are followed only where robots.txt allows;
    on logged-in sites (LinkedIn, X...) Karya reads the user's own view, capped at 30 pages."""
    from . import browser as B
    frontier: list[tuple[int, int, str]] = [(-100, 0, start)]
    frontier += [(-90 - i, 1, _norm_url(s)) for i, s in enumerate(seeds[:10])]
    asked = {u for _, _, u in frontier}
    queued = set(asked)
    rp = None if BROWSER_SITES.search(state["host"]) else _robots(state["root"])[0]
    agent = HEADERS["User-Agent"]
    pages, errors, stop, blocked = [], [], "", 0
    started = time.time()
    try:
        while frontier and len(pages) < max_pages and time.time() - started < 150:
            frontier.sort()
            _, depth, link = frontier.pop(0)
            if rp is not None and link not in asked and not rp.can_fetch(agent, link):
                blocked += 1
                continue
            try:
                social = bool(BROWSER_SITES.search(state["host"]))
                got = B._run("crawl_read", link, 4 if social and depth == 0 else 1)
            except Exception as exc:  # noqa: BLE001 - one page failing never ends the crawl
                errors.append(f"{link}: {type(exc).__name__}")
                continue
            if isinstance(got, str):
                stop = got.replace("ERROR: ", "")[:200]
                break
            if got.get("error"):
                errors.append(f"{link}: {got['error']}")
                continue
            final = str(got.get("url") or link)
            text = str(got.get("text") or "")
            if _WALL.search(urlparse(final).path) or re.search(r"checkpoint/challenge|captcha", final, re.I):
                stop = (f"{urlparse(final).netloc} wants a login or a security check. Log in to it once in Karya's "
                        "browser window (Karya never types passwords here or solves checks), then crawl again.")
                break
            posts = [p for p in got.get("posts") or [] if isinstance(p, dict)]
            row = _page_row(state, final, str(got.get("title") or ""), text, depth, posts)
            pages.append(row)
            post_links = [{"url": p["url"], "text": p.get("said", "")} for p in row.get("posts") or [] if p.get("url")]
            _queue_links(state, frontier, queued, post_links + list(got.get("links") or []), depth)
            time.sleep(2.5)
    finally:
        try:
            B._run("crawl_close")
        except Exception:  # noqa: BLE001
            pass
    if not pages and stop:
        return f"NOT CRAWLED: {stop}"
    extra = {"stopped": stop, "errors": errors[:5], "skipped_by_robots_txt": blocked}
    if BROWSER_SITES.search(state["host"]):
        extra["note"] = (f"Read as the logged-in user, a few seconds a page. Sites like {state['host']} limit "
                         "automated viewing, so Karya reads at most 30 pages per crawl; don't run it in a loop.")
    return _result(state, pages, frontier, "read in Karya's browser (logged in)", extra)


def _safe_fetch(link: str):
    try:
        return requests.get(link, headers=HEADERS, timeout=15)
    except requests.RequestException as exc:
        return type(exc).__name__
