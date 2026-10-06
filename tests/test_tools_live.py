import json

import pytest

from karya.registry import run_tool

pytestmark = pytest.mark.live


def call(_tool, **args):
    out = run_tool(_tool, args)
    assert not out.startswith("ERROR"), out[:500]
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def test_web_search_and_fetch():
    rows = call("web_search", query="python programming language", max_results=5)
    assert rows and rows[0]["url"].startswith("http")
    page = call("fetch_url", url="https://example.com", include_links=True)
    assert page["status"] == 200 and "Example Domain" in page["title"]


def test_news():
    rows = call("news_search", query="Nifty 50", max_results=5)
    assert isinstance(rows, list) and rows[0]["title"] and rows[0]["url"].startswith("http")


def test_stocks():
    quotes = call("stock_quote", symbols=["RELIANCE.NS", "infosys", "AAPL"])
    assert quotes[0]["price"] > 0 and quotes[0]["currency"] == "INR"
    assert quotes[1]["symbol"].endswith(".NS") and quotes[1]["price"] > 0
    assert quotes[2]["price"] > 0
    hist = call("stock_history", symbol="TCS.NS", period="1mo")
    assert hist["points"] and hist["high"] >= hist["low"]
    news = call("stock_news", symbol="RELIANCE.NS", limit=5)
    assert news["news"]
    assert call("find_ticker", query="Tata Motors")
    overview = call("market_overview")
    assert any(r["market"] == "NIFTY 50" and r["price"] for r in overview)


def test_jobs_and_freelance():
    out = call("find_jobs", query="frontend engineer", locations=["United States", "United Kingdom", "Remote"],
               level="entry", limit=15)
    assert out["scanned"] > 50 and out["jobs"], out
    assert {"yc", "hn", "companies"} <= set(out["per_source"]) and out["per_source"]["companies"] > 0
    top = out["jobs"][0]
    assert top["match"] >= out["jobs"][-1]["match"] and top["why"]
    gh = call("find_jobs", query="engineer", sources=["companies"], limit=40)
    board_job = next(j for j in gh["jobs"] if "greenhouse.io" in j["url"])
    detail = call("get_job_details", url=board_job["url"])
    assert detail["title"] and len(detail["description"]) > 200 and detail["form_questions"]
    gigs = call("search_freelance", query="website design", limit=5)
    assert gigs["count"] > 0 and gigs["projects"][0]["url"].startswith("https://")
    li = call("find_jobs", query="frontend developer", locations=["Hyderabad"], sources=["linkedin"], limit=5)
    if not li["jobs"]:
        pytest.skip("LinkedIn returned no results right now (it throttles repeated guest searches)")
    assert li["jobs"][0]["source"] == "LinkedIn"


def test_find_contacts_live():
    out = call("find_contacts", url="https://www.python.org")
    assert out["checked"] >= 2 and (out["emails"] or out["socials"] or out["contact_pages"])
