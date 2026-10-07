"""What people say about a market, and the website crawler. Every HTTP request and browser call is faked.

The user, 2026-10-07: "make a crawler ... crawl and search X, any website, what people are thinking about stocks and
trades" and "it should crawl whatever I say, any site, LinkedIn and all"."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from karya.tools import browser as B
from karya.tools import market_talk as M

NOW = datetime.now(timezone.utc)


def _ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Resp:
    def __init__(self, status=200, body="", ctype="text/html", url="", headers=None):
        self.status_code, self.text, self.url = status, body, url
        self.content = body.encode("utf-8")
        self.headers = {"content-type": ctype, **(headers or {})}
        self.ok = status < 400

    def json(self):
        return json.loads(self.text)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def no_network(*a, **k):
        raise requests.ConnectionError("tests never use the internet")
    monkeypatch.setattr(M.requests, "get", no_network)
    monkeypatch.setattr(M, "_price", lambda asset: None)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    M._CACHE.clear()
    M._RESULTS.clear()
    yield


# ---------------------------------------------------------------- reading the mood
def test_mood_from_words_and_emoji():
    assert M.mood_of("Loaded up more, breakout coming 🚀")[0] == "bullish"
    assert M.mood_of("This will crash, puts printing 📉")[0] == "bearish"
    assert M.mood_of("Earnings on Friday, holding for now")[0] == "neutral"
    assert M.mood_of("not bullish at all, it's weak")[0] == "bearish"          # negation flips it
    assert M.mood_of("तेजी आएगी, खरीद लो")[0] == "bullish"                       # Hindi words count too


def test_levels_people_mention_are_grouped_and_sane():
    posts = [{"text": t} for t in ("Target 1450, SL 1180", "tgt 1452 then 1500", "support at 1195 holding",
                                   "resistance 1300 is key", "target 2027 maybe", "target 100 keep calm")]
    levels = M.levels_in(posts, price=1210.0)
    assert levels["targets"][0] == {"level": 1451, "mentions": 2}              # 1450 and 1452: one level, its middle
    assert levels["stops"] == [{"level": 1180, "mentions": 1}]
    assert levels["support"][0]["level"] == 1195 and levels["resistance"][0]["level"] == 1300
    assert all(x["level"] not in (2027, 100000, 100) for x in levels["targets"])  # a year / "100 keep" / too far
    assert M.levels_in([{"text": "BTC target 150k"}], price=120000)["targets"][0]["level"] == 150000


def test_resolve_knows_markets_and_asks_stocktwits_for_tickers(monkeypatch):
    assert M.resolve("gold price today")["stocktwits"] == "XAUUSD"
    assert M.resolve("BTC")["name"] == "Bitcoin" and M.resolve("bank nifty")["yahoo"] == "^NSEBANK"
    calls = []

    def search(url, params=None, headers=None, timeout=15):
        calls.append(params["q"])
        return Resp(body=json.dumps({"results": [
            {"symbol": "TMPV.NSE", "title": "Tata Motors Passenger Vehicles Ltd", "exchange": "NSE"},
            {"symbol": "TTM", "title": "Tata Motors Ltd.", "exchange": "NYSE"}]}), ctype="application/json")
    monkeypatch.setattr(M, "_get", search)
    asset = M.resolve("what do people think about Tata Motors stock")
    assert calls == ["tata motors"]
    assert asset["stocktwits"] == "TMPV.NSE" and asset["market"] == "india" and asset["yahoo"] == "TMPV.NS"
    assert "tata motors" in asset["terms"] and "tata" not in asset["terms"]    # not every Tata company
    topic = M.resolve("Fed rate cut")
    assert topic["asset"] is False and topic["market"] == "general"


# ---------------------------------------------------------------- each source
STOCKTWITS = {"messages": [
    {"id": 1, "body": "$NVDA breaking out", "created_at": _ago(2), "user": {"username": "trader1"},
     "likes": {"total": 12}, "entities": {"sentiment": {"basic": "Bullish"}}},
    {"id": 2, "body": "$NVDA overvalued here", "created_at": _ago(3), "user": {"username": "bear2"},
     "entities": {"sentiment": {"basic": "Bearish"}}},
    {"id": 3, "body": "$NVDA earnings next week", "created_at": _ago(4), "user": {"username": "plain"}}]}
REDDIT = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><author><name>/u/asha</name></author><category term="stocks" label="r/stocks"/>
<content type="html">&lt;p&gt;Adding more NVDA on this dip&lt;/p&gt; submitted by /u/asha</content>
<id>t3_a</id><link href="https://www.reddit.com/r/stocks/comments/a/x/"/><published>{t}</published>
<title>NVDA dip is a gift</title></entry></feed>""".replace("{t}", _ago(5))
TV = ('<script type="application/prs.init-data+json">' + json.dumps({"k": {"data": {"ideas": {"data": {"items": [
    {"name": "NVDA long into earnings", "description": "breakout above 240", "created_at": _ago(30),
     "chart_url": "https://www.tradingview.com/chart/NVDA/x/", "likes_count": 9, "comments_count": 1,
     "symbol": {"direction": 1}, "user": {"username": "tvuser"}},
    {"name": "How to read candles", "is_education": True, "symbol": {"direction": 0}, "created_at": _ago(30)}]}}}}})
      + "</script>")


def test_sources_are_read_into_posts(monkeypatch):
    pages = {"api.stocktwits.com": Resp(body=json.dumps(STOCKTWITS), ctype="application/json"),
             "www.reddit.com": Resp(body=REDDIT, ctype="application/atom+xml"),
             "www.tradingview.com": Resp(body=TV)}
    monkeypatch.setattr(M, "_get", lambda url, params=None, headers=None, timeout=15: pages[url.split("/")[2]])
    asset = {"name": "NVIDIA Corp", "core": "NVIDIA", "query": "NVDA", "stocktwits": "NVDA", "tradingview": "NVDA",
             "market": "us", "terms": ["nvda", "nvidia"], "cashtag": "NVDA", "asset": True}
    st = M.src_stocktwits(asset, 7)
    assert [p["label"] for p in st] == ["bullish", "bearish", None] and st[0]["author"] == "@trader1"
    assert st[0]["url"] == "https://stocktwits.com/trader1/message/1" and st[0]["likes"] == 12
    rd = M.src_reddit(asset, 7)
    assert rd[0]["text"].startswith("NVDA dip is a gift. Adding more NVDA") and "submitted by" not in rd[0]["text"]
    assert rd[0]["author"] == "u/asha in r/stocks" and rd[0]["time"] is not None
    tv = M.src_tradingview(asset, 7)
    assert len(tv) == 1 and tv[0]["label"] == "bullish" and tv[0]["author"] == "tvuser"   # education skipped


def test_rate_limits_are_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(M, "_get", lambda *a, **k: Resp(status=429, body="slow down"))
    asset = M.resolve("bitcoin")
    with pytest.raises(M.SourceError, match="rate-limiting"):
        M.src_reddit(asset, 7)


def test_x_posts_must_be_about_the_stock():
    asset = {"name": "Reliance Industries Ltd.", "core": "Reliance", "full": "Reliance Industries", "query": "Reliance",
             "stocktwits": "RELIANCE.NSE", "market": "india", "terms": ["reliance"], "cashtag": "RELIANCE",
             "asset": True}
    keep = ["Reliance share looks strong into the Jio IPO", "#RELIANCE breaking out", "Reliance Industries Q2 today",
            "Reliance-Jio merger news"]
    drop = ["India's agricultural self-reliance is vital", "Too much reliance on imports, we must make our own",
            "Over-reliance on social media"]
    for text in keep:
        assert M._relevant(M._post("x", text), asset), text
    for text in drop:
        assert not M._relevant(M._post("x", text), asset), text
    assert M._relevant(M._post("reddit", "Too much reliance on banks"), asset)   # market subreddits: by name is enough
    assert not M._relevant(M._post("linkedin", "Great learning experience at Reliance Industries Limited"), asset)
    assert M._relevant(M._post("linkedin", "Reliance Industries shares after the Jio IPO filing"), asset)
    query = M._x_query(asset, 7)
    assert '"Reliance Industries"' in query and '"Reliance share"' in query and "#RELIANCE" in query
    assert '"Reliance" OR' not in query and "since:" in query


def test_x_needs_the_users_login(monkeypatch):
    asset = M.resolve("bitcoin")
    monkeypatch.setattr(B, "_run", lambda method, *a: {"url": "https://x.com/i/flow/login", "posts": [], "login": True})
    with pytest.raises(M.SourceError, match="not logged in to X"):
        M.src_x(asset, 7)
    seen = []

    def feed(method, url, scrolls):
        seen.append(url)
        return {"url": url, "posts": [{"author": "@a", "text": "$BTC to the moon 🚀", "time": _ago(1),
                                       "url": "https://x.com/a/status/1", "likes": 3},
                                      {"author": "@ad", "text": "Buy BTC on our app", "ad": True}]}
    monkeypatch.setattr(B, "_run", feed)
    posts = M.src_x(asset, 7)
    assert [p["author"] for p in posts] == ["@a"]                                # the ad is dropped
    assert "x.com/search?q=" in seen[0] and "%24BTC" in seen[0] and "since%3A" in seen[0]


# ---------------------------------------------------------------- the whole answer
def _fake_sources(monkeypatch, rows_by_source, broken=()):
    for name in M.SOURCES:
        def make(n):
            def src(asset, days):
                if n in broken:
                    raise M.SourceError(f"{n} is down")
                return [M._post(n, **row) for row in rows_by_source.get(n, [])]
            return src
        monkeypatch.setattr(M, f"src_{name}", make(name))
    monkeypatch.setattr(M, "resolve", lambda q, s="": {
        "name": "Reliance Industries Ltd.", "core": "Reliance", "query": q, "stocktwits": "RELIANCE.NSE",
        "market": "india", "terms": ["reliance"], "cashtag": "RELIANCE", "asset": True})


def test_market_sentiment_counts_quotes_and_never_advises(monkeypatch):
    bull = [{"text": f"Reliance breakout, buying more {i}", "url": f"https://r/{i}", "author": f"u/b{i}",
             "when": _ago(i + 1), "likes": i} for i in range(6)]
    bear = [{"text": "Reliance looks weak, selling", "url": "https://r/x1", "author": "u/s1", "when": _ago(2)},
            {"text": "Reliance breakdown below support at 1195", "url": "https://r/x2", "author": "u/s2",
             "when": _ago(3)}]
    spam = [{"text": f"Reliance to the moon 🚀🚀 {i}", "url": f"https://r/spam{i}", "author": "u/loud",
             "when": _ago(1)} for i in range(5)]
    old = [{"text": "Reliance bullish!!", "url": "https://r/old", "author": "u/o", "when": _ago(24 * 40)}]
    other = [{"text": "Infosys breakout", "url": "https://r/inf", "author": "u/i", "when": _ago(1)}]
    news = [{"text": "Reliance shares jump on Jio IPO news", "url": "https://n/1", "author": "Mint", "when": _ago(2),
             "kind": "news"}]
    _fake_sources(monkeypatch, {"reddit": bull + bear + spam + old + other, "news": news}, broken=("x",))
    out = M.run("Reliance")
    assert out["asking_about"] == "Reliance Industries Ltd. (RELIANCE.NSE)"
    assert out["crowd_mood"].startswith("Mostly bullish: 8 bullish vs 2 bearish")   # the loud account counts twice
    assert out["by_source"]["reddit"]["posts"] == 13                                 # old and off-topic dropped
    assert out["bullish_examples"][0]["url"] and out["bearish_examples"][0]["source"] == "reddit"
    assert out["news"]["headlines"][0]["said"].startswith("Reliance shares jump")
    assert out["not_read"] == {"x": "x is down"}
    assert "not advice" in out["note"]
    assert M.run("Reliance")["cached"].startswith("from ")                           # asked again: no new requests


def test_no_posts_says_how_to_widen(monkeypatch):
    _fake_sources(monkeypatch, {})
    out = M.run("Reliance")
    assert out["crowd_mood"].startswith("Not enough opinions") and "more" in out


# ---------------------------------------------------------------- crawling any website
SITE = {
    "https://shop.example.com/robots.txt": Resp(body="User-agent: *\nDisallow: /private\n", ctype="text/plain"),
    "https://shop.example.com/sitemap.xml": Resp(status=404),
    "https://shop.example.com/": Resp(body='<html><title>Home</title><body><main><p>Welcome to the forum about '
                                           'markets and everything traders discuss every single day here.</p>'
                                           '<a href="/t/reliance-q2">Reliance Q2 thread</a> <a href="/private/x">x</a>'
                                           ' <a href="https://other.com/a">other site</a> <a href="/t/tcs">TCS</a>'
                                           '</main></body></html>'),
    "https://shop.example.com/t/reliance-q2": Resp(body='<html><title>Reliance Q2</title><body><noscript><div>'
                                                       '<p>Reliance Jio numbers look strong this quarter, many are '
                                                       'bullish on Reliance into the IPO and adding on dips.</p>'
                                                       '</div></noscript></body></html>'),
    "https://shop.example.com/t/tcs": Resp(body="<html><body><main><p>TCS margins are under pressure this year "
                                                "and the stock has been weak for a long time now, sadly.</p></main>"
                                                "</body></html>"),
}


def test_crawl_follows_robots_stays_on_site_and_finds_the_topic(monkeypatch):
    fetched = []

    def get(url, params=None, headers=None, timeout=15):
        fetched.append(url)
        resp = SITE.get(url) or Resp(status=404)
        resp.url = url
        return resp
    monkeypatch.setattr(M.requests, "get", get)
    out = M.crawl_site("https://shop.example.com/", "Reliance", 10, use_browser=False)
    assert out["mode"].startswith("read over HTTP") and out["pages_read"] == 3
    assert out["pages"][0]["url"].endswith("/t/reliance-q2")                         # read from its <noscript> copy
    assert "bullish on Reliance" in out["pages"][0]["passages"][0]
    assert not any("private" in u or "other.com" in u for u in fetched)              # robots.txt and other sites
    blocked = M.crawl_site("https://shop.example.com/private/x", "", 5, use_browser=False)
    assert blocked.startswith("NOT CRAWLED") and "robots.txt" in blocked


def test_site_names_start_at_the_sites_own_search():
    url, searched = M._start_url("linkedin", "product manager hiring")
    assert searched and url.startswith("https://www.linkedin.com/search/results/content/?keywords=product+manager")
    assert M._start_url("x", "$NVDA")[0].startswith("https://x.com/search?q=%24NVDA")
    assert M._start_url("https://www.linkedin.com/in/someone", "pm") == ("https://www.linkedin.com/in/someone", False)
    assert M._start_url("moneycontrol.com", "") == ("https://moneycontrol.com", False)


def test_login_sites_are_read_in_the_browser_and_only_posts_are_followed(monkeypatch):
    calls = []
    feed = {"url": "https://www.linkedin.com/search/results/content?keywords=reliance", "title": "Search",
            "text": "Posts\nAsha Rao: Reliance Jio IPO is the biggest listing this year, I'm bullish on Reliance.",
            "links": [{"url": "https://www.linkedin.com/feed/update/urn:li:activity:1/", "text": "post"},
                      {"url": "https://www.linkedin.com/in/asha-rao/", "text": "Asha Rao"},
                      {"url": "https://www.linkedin.com/mynetwork/", "text": "My Network"},
                      {"url": "https://www.linkedin.com/login", "text": "Sign in"}]}
    post = {"url": "https://www.linkedin.com/feed/update/urn:li:activity:1", "title": "Post",
            "text": "Reliance Jio IPO: full thread. Reliance has been rallying on the news.", "links": []}

    def run(method, *args):
        calls.append((method, args[0] if args else None))
        if method == "crawl_close":
            return "closed"
        return feed if "search" in args[0] else post
    monkeypatch.setattr(B, "_run", run)
    out = M.crawl_site("linkedin", "Reliance", 10)
    assert out["mode"] == "read in Karya's browser (logged in)" and out["pages_read"] == 2
    read = [a for m, a in calls if m == "crawl_read"]
    assert read[1].endswith("/feed/update/urn:li:activity:1")                        # the post, not profiles or menus
    assert not any("/in/" in u or "mynetwork" in u or "login" in u for u in read)
    assert calls[-1] == ("crawl_close", None)                                         # its tab is closed
    assert "limit automated viewing" in out["note"]


def test_crawl_lists_the_matching_posts(monkeypatch):
    page = {"url": "https://www.linkedin.com/search/results/content/?keywords=apm", "title": "Search",
            "text": "lots of text about apm roles", "links": [],
            "posts": [{"author": "Asha Rao", "text": "We're hiring an APM in Hyderabad, DM me", "time": "2d",
                       "url": "https://www.linkedin.com/feed/update/urn:li:activity:7/"},
                      {"author": "Ben", "text": "My weekend trip photos", "url": "https://www.linkedin.com/feed/update/x/"}]}
    monkeypatch.setattr(B, "_run", lambda method, *a: "closed" if method == "crawl_close" else page)
    out = M.crawl_site("linkedin", "apm", 1)
    first = out["pages"][0]
    assert first["posts"] == [{"who": "Asha Rao", "when": "2d", "said": "We're hiring an APM in Hyderabad, DM me",
                               "url": "https://www.linkedin.com/feed/update/urn:li:activity:7/"}]
    assert "passages" not in first


def test_a_login_wall_stops_the_browser_crawl(monkeypatch):
    def run(method, *args):
        if method == "crawl_close":
            return "closed"
        return {"url": "https://www.linkedin.com/authwall?trk=x", "title": "Sign in", "text": "Sign in", "links": []}
    monkeypatch.setattr(B, "_run", run)
    out = M.crawl_site("https://www.linkedin.com/company/acme/posts/", "hiring")
    assert out.startswith("NOT CRAWLED") and "Log in to it once in Karya's browser" in out


def test_robots_refusal_reads_only_the_page_the_user_named(monkeypatch):
    monkeypatch.setattr(M.requests, "get", lambda url, **k: Resp(body="User-agent: *\nDisallow: /\n",
                                                                 ctype="text/plain", url=url))
    read = []

    def run(method, *a):
        if method == "crawl_close":
            return "closed"
        read.append(a[0])
        return {"url": a[0], "title": "Page", "text": "A page about bitcoin " * 20,
                "links": [{"url": "https://news.example.com/markets/btc-1", "text": "Bitcoin story"}]}
    monkeypatch.setattr(B, "_run", run)
    out = M.crawl_site("https://news.example.com/markets", "bitcoin", 3)
    assert out["mode"].startswith("read in Karya's browser") and "robots.txt" in out["mode"]
    assert read == ["https://news.example.com/markets"] and out["skipped_by_robots_txt"] == 1
    assert out["pages_with_matches"] == 1
