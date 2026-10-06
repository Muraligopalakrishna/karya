"""Stocks and markets (Yahoo Finance data via yfinance)."""
from __future__ import annotations

import math

import yfinance as yf

from ..registry import P, tool
from .web import news_search

INDICES = {
    "NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "NIFTY BANK": "^NSEBANK", "S&P 500": "^GSPC",
    "NASDAQ": "^IXIC", "DOW JONES": "^DJI", "USD/INR": "INR=X", "GOLD": "GC=F", "BITCOIN": "BTC-USD",
}


def _num(value, digits: int = 2):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else round(value, digits)


def resolve_symbol(query: str) -> str:
    """'reliance' -> 'RELIANCE.NS'. Symbols that already look like tickers are returned as-is."""
    q = query.strip()
    if q.upper() in INDICES:
        return INDICES[q.upper()]
    if q.startswith("^") or "." in q or "=" in q or "-" in q:
        return q.upper()
    try:
        quotes = yf.Search(q, max_results=6).quotes
    except Exception:
        quotes = []
    equities = [x for x in quotes if x.get("quoteType") in ("EQUITY", "ETF", "INDEX", "MUTUALFUND", None)]
    for pick in equities:  # prefer NSE listing for Indian names
        if str(pick.get("symbol", "")).endswith(".NS"):
            return pick["symbol"]
    if equities:
        return equities[0]["symbol"]
    return q.upper()


def quote(symbol: str) -> dict:
    sym = resolve_symbol(symbol)
    t = yf.Ticker(sym)
    hist = t.history(period="5d", interval="1d")
    if hist.empty:
        return {"symbol": sym, "error": "no price data (check the ticker)"}
    last = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
    info = {}
    try:
        fi = t.fast_info
        info = {"currency": fi.get("currency"), "prev_close": _num(fi.get("previousClose")),
                "last": _num(fi.get("lastPrice")), "year_high": _num(fi.get("yearHigh")),
                "year_low": _num(fi.get("yearLow")), "market_cap": _num(fi.get("marketCap"), 0)}
    except Exception:
        pass
    price = info.get("last") or _num(last)
    prev_close = info.get("prev_close") or _num(prev)
    change = _num(price - prev_close) if price and prev_close else None
    pct = _num(change / prev_close * 100) if change is not None and prev_close else None
    return {"symbol": sym, "price": price, "change": change, "change_pct": pct, "prev_close": prev_close,
            "day_high": _num(hist["High"].iloc[-1]), "day_low": _num(hist["Low"].iloc[-1]),
            "currency": info.get("currency"), "52w_high": info.get("year_high"), "52w_low": info.get("year_low"),
            "market_cap": info.get("market_cap"), "as_of": str(hist.index[-1])[:16]}


@tool("stock_quote", "Current price and daily change for one or more stocks, indices, currencies or crypto. "
      "Accepts tickers (TCS.NS, AAPL, ^NSEI) or company names (reliance, infosys).", {
    "symbols": P("array", "Tickers or company names", items={"type": "string"}),
}, required=["symbols"], group="finance")
def stock_quote(symbols: list[str]):
    return [quote(s) for s in symbols[:12]]


@tool("market_overview", "Snapshot of major markets: Nifty 50, Sensex, Bank Nifty, S&P 500, Nasdaq, Dow, USD/INR, gold, bitcoin.",
      group="finance")
def market_overview():
    rows = []
    for name, sym in INDICES.items():
        q = quote(sym)
        rows.append({"market": name, "price": q.get("price"), "change_pct": q.get("change_pct"), "as_of": q.get("as_of")})
    return rows


@tool("stock_history", "Price history summary for a stock over a period.", {
    "symbol": P("string", "Ticker or company name"),
    "period": P("string", "1d,5d,1mo,3mo,6mo,1y,2y,5y,ytd,max (default 1mo)"),
    "interval": P("string", "1m,5m,15m,1h,1d,1wk,1mo (default 1d)"),
}, required=["symbol"], group="finance")
def stock_history(symbol: str, period: str = "1mo", interval: str = "1d"):
    sym = resolve_symbol(symbol)
    hist = yf.Ticker(sym).history(period=period, interval=interval)
    if hist.empty:
        return {"symbol": sym, "error": "no data"}
    closes = hist["Close"]
    first, last = float(closes.iloc[0]), float(closes.iloc[-1])
    step = max(1, len(hist) // 25)
    points = [{"date": str(idx)[:16], "close": _num(row["Close"]), "volume": int(row["Volume"])}
              for idx, row in hist.iloc[::step].iterrows()]
    return {"symbol": sym, "period": period, "start": _num(first), "end": _num(last),
            "change_pct": _num((last - first) / first * 100), "high": _num(hist["High"].max()),
            "low": _num(hist["Low"].min()), "points": points}


@tool("stock_news", "Latest news for a stock/company (Yahoo Finance + web news).", {
    "symbol": P("string", "Ticker or company name"),
    "limit": P("integer", "Max articles (default 8)"),
}, required=["symbol"], group="finance")
def stock_news(symbol: str, limit: int = 8):
    sym = resolve_symbol(symbol)
    items = []
    try:
        for n in yf.Ticker(sym).news[:limit]:
            c = n.get("content") or {}
            url = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
            items.append({"date": (c.get("pubDate") or "")[:16], "title": c.get("title"),
                          "source": (c.get("provider") or {}).get("displayName"), "url": url,
                          "summary": (c.get("summary") or "")[:300]})
    except Exception:
        pass
    if len(items) < 3:
        extra = news_search(f"{symbol} stock", max_results=limit, timelimit="w")
        if isinstance(extra, list):
            items.extend(extra)
    return {"symbol": sym, "news": items[:limit]} if items else f"No news found for {sym}."


@tool("find_ticker", "Look up stock ticker symbols for a company name.", {
    "query": P("string", "Company name"),
}, required=["query"], group="finance")
def find_ticker(query: str):
    quotes = yf.Search(query, max_results=8).quotes
    return [{"symbol": q.get("symbol"), "name": q.get("shortname") or q.get("longname"),
             "exchange": q.get("exchange"), "type": q.get("quoteType")} for q in quotes] or "No matches."
