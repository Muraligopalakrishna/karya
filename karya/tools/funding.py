"""Recently funded companies: who just raised money (and so is hiring), ranked for the user.

find_funded_companies reads funding news in any country - Google News (US, India, UK and other editions), TechCrunch,
Crunchbase News, tech.eu, Inc42, YourStory - plus Y Combinator's latest batches. From each headline it takes the
company, amount, round, date, investors, field and country. It ranks the companies for the user (a fresh round,
Series A-C, their country and field, open roles that fit), finds each top company's careers page, and lists the
matching jobs with ids like find_jobs (J12...), so choose_jobs and the apply flow work the same way."""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..registry import P, tool
from . import job_sources as S
from . import jobs as J

FEEDS = [("TechCrunch", "https://techcrunch.com/category/venture/feed/", None),
         ("Crunchbase News", "https://news.crunchbase.com/feed/", None),
         ("tech.eu", "https://tech.eu/feed/", "europe"),
         ("Inc42", "https://inc42.com/feed/", "india"),
         ("YourStory", "https://yourstory.com/feed", "india")]
EDITIONS = {"united states": "US", "india": "IN", "united kingdom": "GB", "canada": "CA", "australia": "AU",
            "singapore": "SG", "ireland": "IE", "united arab emirates": "AE", "south africa": "ZA", "nigeria": "NG",
            "kenya": "KE", "philippines": "PH", "malaysia": "MY", "pakistan": "PK", "new zealand": "NZ", "israel": "IL"}
YC_DEAL_USD = 500_000

# ------------------------------------------------------------------ reading a funding headline
_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]\s*|(?:exclusive|breaking|funding alert|funding news|funding|scoop|report|"
                     r"update|deals?|news|watch|just in)\s*[:\-–—|]\s*)+", re.I)
_VERB = (r"(?:raises|raised|raise|secures|secured|secure|bags|bagged|lands|landed|closes|closed|nabs|snags|picks up|"
         r"picked up|scores|gets|receives|received|grabs|attracts|announces|completes|completed|wins|draws|rakes in|hauls in)")
_SPLIT = re.compile(rf"^(?P<subj>.+?)\s+{_VERB}\s+(?P<rest>.+)$", re.I)
_FUNDISH = re.compile(r"[$€£₹]|\b(?:rs\.?|inr|usd|eur|gbp|funding|round|seed|series|investment|capital|financing|"
                      r"backing|debt|million|billion|crores?|mn|bn)\b", re.I)
_VC_RAISE = re.compile(r"\b(?:new|first|second|third|fourth|fifth|sixth|debut|maiden|flagship|opportunity|venture|vc|"
                       r"early-stage|growth-stage|seed-stage)\s+fund\b|\bfund\s+(?:[ivx]+|\d+)\b|\bfunds\b|"
                       r"\b(?:million|billion|mn|bn|[mbk])\s+(?:\w+\s+)?fund\b|\bto back\b|\bto invest in\b|"
                       r"\bfor startups\b|\blimited partners\b|\blps\b", re.I)
_NOT_FUNDING = re.compile(r"^(?:\S+\s+){0,3}(?:fines?|fined|penalty|lawsuit|settlement|in revenues?|in sales|"
                          r"revenues?|contracts?|orders?|tender|tax|refund|valuation cut)\b", re.I)
_ROUNDUP = re.compile(r"\b(?:startups|companies|firms)\b.*\b(?:raised|raise|raises)\b|\bthis week\b|\bweekly\b|"
                      r"\bfunding roundup\b|\bas many as\b|\btop \d+\b|\bweek in\b", re.I)
_AMOUNT = re.compile(r"(?P<cur>US\$|USD|\$|€|EUR|£|GBP|₹|Rs\.?|INR|CAD|C\$|A\$|AUD|S\$|SGD)\s?(?P<num>\d+(?:[.,]\d+)*)\s?"
                     r"(?P<unit>thousand|million|billion|crores?|lakhs?|mn|mln|mil|bn|cr|k|m|b|l)?(?![a-z])", re.I)
RATES = {"$": 1, "us$": 1, "usd": 1, "€": 1.08, "eur": 1.08, "£": 1.27, "gbp": 1.27, "₹": 0.012, "rs": 0.012,
         "rs.": 0.012, "inr": 0.012, "cad": 0.73, "c$": 0.73, "a$": 0.66, "aud": 0.66, "s$": 0.74, "sgd": 0.74}
UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mln": 1e6, "mil": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
         "billion": 1e9, "cr": 1e7, "crore": 1e7, "crores": 1e7, "l": 1e5, "lakh": 1e5, "lakhs": 1e5}
CURRENCY_COUNTRY = {"₹": "india", "rs": "india", "rs.": "india", "inr": "india", "£": "united kingdom",
                    "gbp": "united kingdom", "€": "europe", "eur": "europe", "s$": "singapore", "sgd": "singapore",
                    "a$": "australia", "aud": "australia", "c$": "canada", "cad": "canada"}
SOURCE_COUNTRY = [(re.compile(r"inc42|entrackr|yourstory|economic times|\bet\b|moneycontrol|business standard|livemint|"
                              r"\bmint\b|vccircle|businessline|financial express|indian startup|startuptalky|techcircle|"
                              r"hindustan times|times of india|news18|business today|analytics india|fortune india|"
                              r"bw disrupt|businessworld|the hindu|ndtv|deccan|medianama|trak\.in|indianweb2", re.I), "india"),
                  (re.compile(r"uktn|uktech|city a\.?m|business ?cloud|bdaily|prolific north|tech nation", re.I), "united kingdom"),
                  (re.compile(r"tech\.eu|sifted|eu-startups|silicon canals|maddyness|trending topics|"
                              r"deutsche startups|gruenderszene|gründerszene", re.I), "europe")]
_ROUND = re.compile(r"\b(pre[- ]?seed|seed|angel|pre[- ]series [a-h]|series [a-h]\+?|bridge|growth|venture debt|"
                    r"debt|ipo|strategic|grant)\b", re.I)
_DESCRIPTORS = {"startup", "start-up", "firm", "company", "platform", "maker", "provider", "developer", "unicorn",
                "player", "brand", "app", "outfit", "specialist", "business", "venture", "scaleup", "scale-up", "spinout",
                "spin-off", "operator", "marketplace", "lender", "neobank", "insurtech", "fintech", "edtech",
                "healthtech", "proptech", "agritech", "agtech", "biotech", "medtech", "cleantech", "deeptech", "saas",
                "manufacturer", "producer", "retailer", "creator", "builder", "studio", "lab"}
_BAD = {"team", "founder", "founders", "co-founder", "cofounder", "alumni", "startups", "companies", "investors", "firms",
        "fund", "funds", "vc", "vcs", "partners", "former", "executives", "engineers", "researchers", "students", "it",
        "this", "that", "they", "who", "how", "why", "what", "when", "report", "sources", "startup", "company", "firm",
        "government", "unicorns", "here", "these", "those", "its", "his", "her", "their", "we", "i"}
DEMONYMS = {"indian": "india", "american": "united states", "british": "united kingdom", "scottish": "united kingdom",
            "german": "germany", "french": "france", "danish": "denmark", "swedish": "sweden", "dutch": "netherlands",
            "spanish": "spain", "italian": "italy", "polish": "poland", "irish": "ireland", "swiss": "switzerland",
            "austrian": "austria", "belgian": "belgium", "finnish": "finland", "norwegian": "norway",
            "portuguese": "portugal", "ukrainian": "ukraine", "estonian": "estonia", "greek": "greece", "czech": "czechia",
            "romanian": "romania", "egyptian": "egypt", "nigerian": "nigeria", "kenyan": "kenya",
            "south african": "south africa", "israeli": "israel", "canadian": "canada", "australian": "australia",
            "singaporean": "singapore", "emirati": "united arab emirates", "saudi": "saudi arabia", "brazilian": "brazil",
            "mexican": "mexico", "argentine": "argentina", "colombian": "colombia", "chilean": "chile",
            "japanese": "japan", "korean": "south korea", "chinese": "china", "taiwanese": "taiwan",
            "pakistani": "pakistan", "bangladeshi": "bangladesh", "indonesian": "indonesia", "vietnamese": "vietnam",
            "thai": "thailand", "filipino": "philippines", "malaysian": "malaysia", "turkish": "turkey"}
SECTORS = {"AI": r"\bai\b|artificial intelligence|machine learning|\bllms?\b|genai|generative|agentic|\bagents?\b",
           "Fintech": r"fintech|payments?|banking|neobank|lending|lender|credit|insurtech|insurance|wealth|trading|"
                      r"crypto|defi|stablecoin",
           "SaaS / B2B": r"\bsaas\b|\bb2b\b|enterprise software|workflow|productivity",
           "Developer tools": r"developer|devtools|\bapi\b|open[- ]source|infrastructure|\bcloud\b|database",
           "Health": r"health|medtech|clinic|hospital|patient|medical|\bcare\b|pharma|drug",
           "Biotech": r"biotech|genomic|life sciences",
           "Edtech": r"edtech|education|learning|tutor|school|students?",
           "Climate / energy": r"climate|energy|\bevs?\b|electric vehicle|battery|solar|carbon|cleantech|renewable|grid",
           "E-commerce / D2C": r"e-?commerce|\bd2c\b|retail|marketplace|commerce|consumer brand",
           "Logistics": r"logistics|supply chains?|freight|shipping|delivery|maritime|warehouse",
           "Security": r"security|cyber",
           "Gaming": r"gaming|\bgames?\b|esports",
           "HR / work": r"\bhr\b|hiring|recruit|payroll|workforce|talent",
           "Legal": r"legal|\blaw\b|compliance",
           "Real estate": r"real estate|proptech|housing|homeowners?|property",
           "Mobility": r"mobility|automotive|vehicles?|\bcars?\b|ride-?hailing",
           "Deep tech": r"robot|quantum|semiconductor|\bchips?\b|\bspace\b|aerospace|satellite|drone|defen[cs]e|"
                        r"physical ai|hardware|computer\b",
           "Media / creator": r"\bmedia\b|creator|social|music|video",
           "Food / agri": r"\bfood\b|agri|farm|restaurant"}
ROUND_POINTS = {"pre-seed": 6, "seed": 9, "angel": 5, "series a": 15, "series b": 16, "series c": 13, "series d": 9,
                "series e": 7, "series f": 6, "series g": 5, "series h": 5, "growth": 8, "bridge": 6, "strategic": 6,
                "debt": 2, "grant": 3, "ipo": 2, "yc": 9}


def _bare(token: str) -> str:
    return re.sub(r"(?:'s|’s)$", "", token.lower().strip(" ,.:;'’\"()[]"))


def _money(usd: float) -> str:
    if usd >= 1e9:
        return f"${usd / 1e9:.1f}B".replace(".0B", "B")
    if usd >= 1e6:
        return f"${usd / 1e6:.1f}M".replace(".0M", "M")
    return f"${usd / 1e3:.0f}K"


def parse_amount(text: str) -> tuple[str, float] | None:
    """'$11.3M' -> ('$11.3M', 11300000.0); 'Rs 425 Cr' -> ('₹425 Cr (~$51M)', 51000000.0). A valuation
    ('at $1.4B valuation', 'valued at...') is skipped: it isn't the amount raised."""
    for m in _AMOUNT.finditer(text or ""):
        before = (text[max(0, m.start() - 14):m.start()]).lower()
        after = (text[m.end():m.end() + 14]).lower()
        if "valuation" in after or re.search(r"\b(?:at|valued at|valuation of|worth)\s*$", before):
            continue
        cur = m.group("cur").lower()
        try:
            num = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        unit = (m.group("unit") or "").lower()
        usd = num * UNITS.get(unit, 1) * RATES.get(cur, 1)
        if usd < 10_000:
            continue                                  # a price in the headline, not a round
        shown = m.group(0).strip()
        if RATES.get(cur, 1) != 1:
            shown = re.sub(r"^(?:rs\.?|inr)\s?", "₹", shown, flags=re.I) + f" (~{_money(usd)})"
        return shown, usd
    return None


def _currency(text: str) -> str:
    m = _AMOUNT.search(text or "")
    return m.group("cur").lower() if m else ""


def _round(text: str) -> str | None:
    m = _ROUND.search(text or "")
    if not m:
        return None
    r = re.sub(r"\s+", " ", m.group(1).lower().replace("pre seed", "pre-seed").replace("preseed", "pre-seed"))
    if r == "venture debt":
        r = "debt"
    if r.startswith("pre-series") or r.startswith("pre series"):
        return "Pre-Series " + r[-1].upper()
    if r.startswith("series"):
        return "Series " + r.split()[1].upper()
    return r.capitalize()


def _investors(rest: str) -> str | None:
    m = re.search(r"\b(?i:led by) ([A-Z0-9][\w&.'’ -]{1,70}?)(?=\s*(?:,|;|\.|$| with | and others| to | for | at | in | as |\(| - ))", rest)
    if m:
        return m.group(1).strip()
    m = re.search(r"\bfrom ((?:[A-Z][\w&.'’-]*)(?: (?:[A-Z0-9][\w&.'’-]*|and|&))*)", rest)
    if m:
        who = m.group(1).strip()
        if len(who.split()) > 1 or not S.place_key(who):
            return who
    return None


def _country(text: str) -> str | None:
    low = (text or "").lower()
    for word, key in DEMONYMS.items():
        if S.has_place(low, word):
            return key
    m = re.search(r"([A-Z][\w.]*(?: [A-Z][\w.]*)?)-based", text or "")
    if m and S.place_key(m.group(1)):
        return S.place_key(m.group(1))
    for key, c in S.COUNTRIES.items():
        for alias in (key, *c["aka"], *c["cities"]):
            if len(alias) > 2 and S.has_place(low, alias):
                return key
    return None


def sectors_of(text: str) -> list[str]:
    low = (text or "").lower()
    return [name for name, pat in SECTORS.items() if re.search(pat, low)]


def _source_country(source: str) -> str | None:
    """Indian, UK and European tech outlets mostly cover their own startups."""
    return next((country for rx, country in SOURCE_COUNTRY if rx.search(source or "")), None)


def _valid_name(name: str) -> bool:
    toks = name.split()
    if not 1 <= len(toks) <= 5 or not re.match(r"[A-Z0-9]", name):
        return False
    if {_bare(t) for t in toks} & _BAD:
        return False
    low = name.lower()
    return not (S.place_key(low) or low in DEMONYMS or low in S.REGIONS)


def _name_from(subject: str, rest: str) -> str | None:
    s = _PREFIX.sub("", subject).strip(" :-–—|")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"^.*?\b[\w.&'’-]+-backed\s+", "", s, flags=re.I)        # "a16z-backed EliseAI" -> "EliseAI"
    toks = s.split()
    last = max((i for i, t in enumerate(toks) if _bare(t) in _DESCRIPTORS), default=-1)
    if last >= 0:
        toks = toks[last + 1:]                                            # "Physical AI startup SiMa.ai" -> "SiMa.ai"
    owner = max((i for i, t in enumerate(toks) if re.search(r"(?:'s|’s|s'|s’)$", t)), default=-1)
    if 0 <= owner < len(toks) - 1:
        toks = toks[owner + 1:]                                           # "Copenhagen’s Pandektes" -> "Pandektes"
    name = " ".join(toks).strip(" ,.:;'’\"-–—")
    if _valid_name(name):
        return name
    m = re.match(r"^\S+(?:\s+(?:million|billion|mn|bn|crores?|cr|m|k))?\s+(?:for|to fund|into)\s+"
                 r"([A-Z][\w.&'’-]*(?:\s+[A-Z][\w.&'’-]*){0,3})", rest)   # "raises $11M for Ghost, maker of..."
    if m and _valid_name(m.group(1)):
        return m.group(1)
    return None


def parse_headline(title: str) -> dict | None:
    """A funding headline -> {name, amount, usd, round, investors, sectors, country} (None if it isn't one company
    raising money: VC funds, weekly round-ups, nameless 'Swedish startup raises...')."""
    t = re.sub(r"\s+", " ", (title or "")).strip()
    if not t or _ROUNDUP.search(t):
        return None
    m = _SPLIT.match(_PREFIX.sub("", t))
    if not m:
        return None
    subject, rest = m.group("subj"), m.group("rest")
    if not _FUNDISH.search(rest[:80]) or _VC_RAISE.search(rest[:60]) or _NOT_FUNDING.search(rest):
        return None
    name = _name_from(subject, rest)
    if not name:
        return None
    amount = parse_amount(rest)
    return {"name": name, "amount": amount[0] if amount else None, "usd": amount[1] if amount else None,
            "round": _round(rest), "investors": _investors(rest), "sectors": sectors_of(t),
            "country": _country(subject)}


# ------------------------------------------------------------------ the news
def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _iso(date_text: str) -> str:
    try:
        return parsedate_to_datetime(date_text).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return ""


def _items(xml: bytes, label: str, default_country: str | None) -> list[dict]:
    out = []
    for it in BeautifulSoup(xml, "xml").select("item")[:120]:
        title = _text(it.find("title"))
        src = it.find("source")
        source = _text(src) if src else label
        if src and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        title = re.sub(r"(?:\s+-)+\s*$", "", title).strip()
        out.append({"title": title, "link": _text(it.find("link")), "date": _iso(_text(it.find("pubDate"))),
                    "source": source, "default_country": default_country})
    return out


def _gnews(query: str, gl: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-{gl}&gl={gl}&ceid={gl}:en"


def _news_urls(days: int, countries: list[str], sectors: list[str]) -> list[tuple[str, str, str | None]]:
    days = max(1, min(int(days or 30), 90))
    base = [f'startup raises ("seed" OR "Series A" OR "Series B" OR "Series C" OR "pre-seed") when:{days}d',
            f"startup secures funding round when:{days}d"]
    base += [f"{s} startup raises funding when:{days}d" for s in sectors[:2]]
    urls = []
    keys = countries or ["united states", "india", "united kingdom"]
    for key in keys[:4]:
        gl = EDITIONS.get(key)
        for q in base:
            if gl:
                urls.append((f"Google News {gl}", _gnews(q, gl), None))   # editions carry world news too
            else:                                                 # no English edition: name the country instead
                urls.append(("Google News", _gnews(f"{q} {key}", "US"), None))
    return urls + list(FEEDS)


def _yc_recent(batches: int = 2) -> list[dict]:
    meta = S._get_json("https://yc-oss.github.io/api/meta.json", timeout=20).get("batches") or {}
    months = {"winter": 1, "spring": 4, "summer": 6, "fall": 9}
    dated = []
    for slug, b in meta.items():
        m = re.match(r"(winter|spring|summer|fall)\s+(\d{4})", str((b or {}).get("name") or slug).lower().replace("-", " "))
        if m:
            start = datetime(int(m.group(2)), months[m.group(1)], 1, tzinfo=timezone.utc)
            if start <= datetime.now(timezone.utc) + timedelta(days=30):
                dated.append((start, b))
    out = []
    for start, b in sorted(dated, key=lambda x: x[0], reverse=True)[:batches]:
        for c in S._get_json(b.get("api"), timeout=25) or []:
            if not c.get("isHiring"):
                continue
            regions = c.get("regions") or []
            country = next((S.place_key(r) for r in regions if S.place_key(r)), None)
            tags = " ".join([c.get("industry") or "", *(c.get("tags") or []), c.get("one_liner") or ""])
            out.append({"name": c.get("name"), "round": f"YC {b.get('name')}", "amount": "$500K (YC)",
                        "usd": YC_DEAL_USD, "date": start.strftime("%Y-%m-%d"), "country": country,
                        "sectors": sectors_of(tags), "about": c.get("one_liner"), "website": c.get("website"),
                        "team_size": c.get("team_size"), "source": "Y Combinator",
                        "news": f"https://www.ycombinator.com/companies/{c.get('slug')}",
                        "careers": f"https://www.ycombinator.com/companies/{c.get('slug')}/jobs", "yc": True})
    return out


def _merge(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for r in rows:
        key = S.norm_name(r["name"])
        if not key:
            continue
        old = best.get(key)
        if not old:
            best[key] = dict(r, links=[r["news"]] if r.get("news") else [])
            continue
        if r.get("news") and r["news"] not in old["links"]:
            old["links"] = (old["links"] + [r["news"]])[:3]
        for field in ("amount", "usd", "round", "investors", "country", "about", "website"):
            if not old.get(field) and r.get(field):
                old[field] = r[field]
        old["sectors"] = list(dict.fromkeys((old.get("sectors") or []) + (r.get("sectors") or [])))
        if (r.get("date") or "") > (old.get("date") or ""):
            old["date"] = r["date"]
    return list(best.values())


def funded_companies(days: int = 30, countries: list[str] | None = None, sectors: list[str] | None = None,
                     include_yc: bool = True) -> list[dict]:
    """Companies that raised money recently, from the news (and YC's latest batches), merged by company."""
    keys = sorted({k for p in countries or [] for k in ([S.place_key(p)] if S.place_key(p) else S.region_countries(p))})
    sectors = list(sectors or [])

    def build():
        urls = _news_urls(days, keys, sectors)

        def read(entry):
            label, url, country = entry
            return _items(S._get(url, timeout=15, headers=S.HEADERS).content, label, country)

        items = [it for got in S.pmap(read, urls, workers=12, budget=15) if got for it in got]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = []
        for it in items:
            if it["date"] and it["date"] < cutoff:
                continue
            got = parse_headline(it["title"])
            if not got:
                continue
            got["country"] = (got["country"] or _source_country(it["source"]) or it["default_country"]
                              or CURRENCY_COUNTRY.get(_currency(it["title"])) or _country(it["title"]))
            rows.append({**got, "date": it["date"], "news": it["link"], "source": it["source"], "headline": it["title"]})
        if include_yc:
            rows += S._try(lambda n: _yc_recent(n), 2) or []
        return _merge(rows)

    return [dict(c) for c in S._cached(("funded", days, tuple(keys), tuple(sectors), include_yc), 3 * 3600, build)]


# ------------------------------------------------------------------ ranking
def _age(date: str) -> float | None:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(date + "T00:00:00+00:00")).days
    except (TypeError, ValueError):
        return None


def score_company(c: dict, countries: set[str], sectors: list[str], roles: list[list[str]] | None = None) -> tuple[int, list[str]]:
    score, why = 20, []
    age = _age(c.get("date") or "")
    if age is not None and not c.get("yc"):
        pts = 22 if age <= 7 else 16 if age <= 14 else 10 if age <= 30 else 4 if age <= 60 else 0
        score += pts
        why.append("raised today" if age < 1 else f"raised {int(age)}d ago")
    elif c.get("yc"):
        score += 8 if age is not None and age <= 120 else 4
        why.append(c.get("round") or "YC")
    rnd = (c.get("round") or "").lower()
    key = "yc" if rnd.startswith("yc") else rnd.replace("pre-series", "series")
    score += ROUND_POINTS.get(key, 5)
    if rnd and not rnd.startswith("yc"):
        why.append(c["round"] + (" (hiring stage)" if key in ("series a", "series b", "series c") else ""))
    if c.get("usd"):
        score += int(max(0, min(14, 3 * math.log10(max(c["usd"], 1e5) / 1e5))))
        if not c.get("yc"):
            why.append(_money(c["usd"]))
    if countries:
        country = c.get("country")
        inside = country in countries or (country == "europe" and countries & set(S.EUROPE))
        if inside:
            score += 12
            why.append(f"in {country.title()}")
        elif country:
            score -= 6
    if sectors:
        wanted = {s.lower() for s in sectors}
        hit = [s for s in c.get("sectors") or [] if any(w in s.lower() or s.lower() in w for w in wanted)]
        if hit:
            score += 10
            why.append("in your field (" + ", ".join(hit[:2]) + ")")
    if c.get("open_roles"):
        score += 6
        fits = len(c.get("matching") or [])
        if fits:
            score += min(18, 8 + 3 * fits)
            why.append(f"{fits} open role{'s' if fits != 1 else ''} fit you")
        else:
            why.append(f"{c['open_roles']} open roles")
    elif c.get("careers"):
        score += 2
    return max(1, min(99, score)), why


def rank_companies(companies: list[dict], countries: set[str], sectors: list[str]) -> list[dict]:
    for c in companies:
        c["score"], c["why"] = score_company(c, countries, sectors)
    return sorted(companies, key=lambda c: (c["score"], c.get("date") or ""), reverse=True)


def _distinctive(name: str) -> bool:
    """Safe to guess its board address: several words, inner capitals or digits (not a plain word like 'Ghost')."""
    return len(name.split()) > 1 or bool(re.search(r".[A-Z]|\d|\.", name))


def _funding_note(c: dict) -> dict:
    return {k: v for k, v in {"company": c.get("name"), "round": c.get("round"), "amount": c.get("amount"),
                              "date": c.get("date"), "news": (c.get("links") or [c.get("news")])[0]}.items() if v}


def _role_match(title: str, roles: list[list[str]]) -> bool:
    words = set(re.findall(r"[a-z0-9+#.]+", J.norm(title or "")))
    return not roles or any(sum(1 for t in role if t in words) / len(role) >= (1.0 if len(role) <= 2 else 0.66)
                            for role in roles)


def attach_careers(companies: list[dict], query: str, locations, budget: float = 35) -> None:
    """Find each company's careers board (and remember it), count its open roles and the ones that fit the user."""
    roles = [J.terms(r) for r in (query or "").split(",") if J.terms(r)]
    role = (S.roles_of(query, 1) or [""])[0]

    def one(c):
        if c.get("yc"):
            return None
        hint = " ".join((c.get("sectors") or [])[:1])
        info = S.resolve_company(c["name"], hint=hint, website=c.get("website") or "", guess=_distinctive(c["name"]))
        if not info:
            return None
        rows = S.fetch_board(info, role, locations) if info.get("ats") else []
        return info, rows

    for c, got in zip(companies, S.pmap(one, companies, workers=6, budget=budget)):
        if not got:
            continue
        info, rows = got
        c["careers"] = info.get("url") or c.get("careers")
        c["ats"] = info.get("ats")
        if rows:
            note = _funding_note(c)
            for r in rows:
                r["funding"] = note
                r["ctype"] = r.get("ctype") or "startup"
            c["open_roles"] = len(rows)
            c["matching"] = [r for r in rows if _role_match(r.get("title"), roles)]


_LAST: dict = {"time": 0.0, "by_name": {}}


def _remember_for_tags(companies: list[dict]) -> None:
    _LAST["time"] = time.time()
    _LAST["by_name"] = {S.norm_name(c["name"]): _funding_note(c) for c in companies if c.get("name")}


def tag_rows(rows: list[dict]) -> None:
    """Mark jobs at companies that recently raised money (from the latest funding scan; no network)."""
    if time.time() - _LAST["time"] > 24 * 3600 or not _LAST["by_name"]:
        return
    for r in rows:
        if not r.get("funding"):
            note = _LAST["by_name"].get(S.norm_name(r.get("company") or ""))
            if note:
                r["funding"] = note


def _places(countries, prefs) -> tuple[list[str], set[str], bool]:
    """(places, country keys, strict): explicit countries filter strictly; saved job locations only rank."""
    explicit = [c for c in countries or [] if str(c).strip()]
    places = explicit or S.named_places(prefs.get("locations") or [])
    keys = S.wanted_countries(places)
    return places, keys, bool(explicit)


def src_funded(query: str, locations: list[str], level=None, ctypes=None, budget: float = 15) -> list[dict]:
    """find_jobs source: jobs at the best-ranked recently funded companies."""
    if ctypes and not S.type_ok("startup", ctypes):
        return []
    prefs = J.job_preferences()
    places = S.named_places(locations)
    keys = S.wanted_countries(places)
    sectors = list(prefs.get("industries") or [])
    companies = [c for c in funded_companies(45, places, sectors) if not c.get("yc")]
    ranked = rank_companies(companies, keys, sectors)
    _remember_for_tags(ranked)
    top = ranked[:12]
    attach_careers(top, query, locations, budget=budget)
    return [r for c in top for r in (c.get("matching") or [])]


@tool("find_funded_companies", "Companies that just raised money (they're hiring), from funding news in ANY country "
      "(Google News editions, TechCrunch, Crunchbase News, tech.eu, Inc42, YourStory) and Y Combinator's latest "
      "batches. Ranks them for the user (fresh rounds, Series A-C, their country and field, open roles that fit), "
      "finds each top company's careers page and lists matching jobs with ids like find_jobs (choose_jobs works).", {
    "query": P("string", "Role to match in their open jobs (default: the user's saved roles)"),
    "countries": P("array", "Where the companies are, e.g. ['India','United States','UK','Europe'] (default: the "
                            "user's job locations, used for ranking only)", items={"type": "string"}),
    "sectors": P("array", "Fields, e.g. ['AI','fintech','SaaS','health']", items={"type": "string"}),
    "days": P("integer", "Funded in the last N days (default 30, max 90)"),
    "stages": P("array", "Only these rounds, e.g. ['seed','series a','series b']", items={"type": "string"}),
    "limit": P("integer", "How many companies to show (default 15, max 30)"),
    "with_jobs": P("boolean", "Find each top company's careers page and open roles (default true)"),
    "include_yc": P("boolean", "Include Y Combinator's latest batches (default true)"),
}, group="jobs")
def find_funded_companies(query: str = "", countries: list[str] | None = None, sectors: list[str] | None = None,
                          days: int = 30, stages: list[str] | None = None, limit: int = 15, with_jobs: bool = True,
                          include_yc: bool = True):
    prefs = J.job_preferences()
    query = query or ", ".join(prefs.get("roles") or []) or "software engineer"
    places, keys, strict = _places(countries, prefs)
    sectors = list(sectors or prefs.get("industries") or [])
    days = max(1, min(int(days or 30), 90))
    limit = max(1, min(int(limit or 15), 30))
    companies = funded_companies(days, places if strict else [], sectors, include_yc)
    if strict and keys:
        companies = [c for c in companies if c.get("country") in keys
                     or (c.get("country") == "europe" and keys & set(S.EUROPE))]
    if stages:
        wanted = [s.lower().replace("_", " ").strip() for s in stages]
        companies = [c for c in companies if any(w in (c.get("round") or "").lower() for w in wanted)]
    ranked = rank_companies(companies, keys, sectors)
    _remember_for_tags(ranked)
    top = ranked[:limit]
    if with_jobs:
        attach_careers(top[: min(limit, 12)], query, places)
        top = rank_companies(top, keys, sectors)
    jobs_found = [r for c in top for r in (c.get("matching") or [])]
    if jobs_found:
        locs = places if strict else (prefs.get("locations") or [])
        level = prefs.get("level") or "entry"
        merged = dict(prefs, locations=locs, level=level)
        q_terms = [J.terms(r) for r in query.split(",") if J.terms(r)]
        ranked_jobs = J.rank_found(jobs_found, q_terms, locs, bool(prefs.get("remote", True)), 90, merged)
        rows = [J.job_row(*x) for x in ranked_jobs[:40]]
        J.remember_jobs(rows, ranked_jobs)
        by_url = {r["url"]: r for r in rows}
    else:
        by_url = {}
    out_companies = []
    for i, c in enumerate(top, 1):
        row = {"rank": i, "company": c["name"], "round": c.get("round"), "amount": c.get("amount"),
               "date": c.get("date"), "country": (c.get("country") or "").title() or None,
               "sectors": c.get("sectors") or None, "investors": c.get("investors"), "about": c.get("about"),
               "match": c["score"], "why": "; ".join(c["why"]), "news": (c.get("links") or [c.get("news")])[0],
               "careers": c.get("careers"), "open_roles": c.get("open_roles")}
        jobs = [by_url[r["url"]] for r in c.get("matching") or [] if r.get("url") in by_url][:4]
        if jobs:
            row["jobs"] = [{"id": j["id"], "title": j["title"], "location": j.get("location"), "url": j["url"]} for j in jobs]
        out_companies.append({k: v for k, v in row.items() if v not in (None, "", [])})
    out = {"found": len(companies), "shown": len(out_companies), "days": days, "query": query,
           "countries": [str(p) for p in places] or ["anywhere"], "companies": out_companies,
           "jobs_with_ids": sum(len(c.get("jobs") or []) for c in out_companies),
           "next": ("Show the ranked companies. For the listed jobs use choose_jobs (their ids), then apply as usual. "
                    "Companies without listed roles: open their careers page, or find_contacts on their website for a "
                    "short, personal note (one email per company).")}
    if not companies:
        out["more"] = "Nothing matched. Try more days (e.g. 60), fewer filters, or other countries."
    elif places and not strict:
        out["note"] = "Companies in the user's job locations rank higher; other countries are included."
    while len(json.dumps(out, ensure_ascii=False)) > 9_500 and len(out["companies"]) > 5:
        out["companies"].pop()
    return out
