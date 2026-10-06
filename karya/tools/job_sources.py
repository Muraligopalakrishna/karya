"""More places to find jobs, in any country.

find_jobs (jobs.py) uses these on top of its own sources:
- Workday, the careers system of Salesforce, Adobe, Nvidia, Cisco, Accenture, PwC, Walmart, Citi, Barclays and many
  more. Karya searches it through the same job-search API the career pages use, with Workday's own country/city filters.
- SmartRecruiters: Bosch, ServiceNow, Freshworks, PhonePe, Swiggy, Canva, Wise, Deloitte, Grab, Delivery Hero...
- Big tech career APIs: Amazon, Microsoft, Google, Netflix, Atlassian.
- India boards: Instahyre, Cutshort, foundit.  US/EU boards: The Muse, Arbeitnow.
- Discovery: a web search finds company job pages on Workday / Greenhouse / Lever / Ashby / SmartRecruiters / Workable /
  Recruitee for the role and place. Karya reads those companies' boards and remembers them for later searches.

Rows have the same shape as the sources in jobs.py (source, title, company, location, posted, url, apply, text...) plus
`ats` (the careers system), `direct` (True = the company's own careers site) and `ctype` (product / service /
enterprise / startup / staffing, when known)."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache

import requests

from . import jobs as J
from .web import HEADERS

JSON_HEADERS = {**HEADERS, "Accept": "application/json"}


# ------------------------------------------------------------------ http + small helpers
def _get(url: str, params: dict | None = None, timeout: int = 15, headers: dict | None = None) -> requests.Response:
    resp = requests.get(url, params=params, headers=headers or JSON_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp


def _get_json(url: str, params: dict | None = None, timeout: int = 15):
    return _get(url, params, timeout).json()


def _get_text(url: str, params: dict | None = None, timeout: int = 20) -> str:
    return _get(url, params, timeout, headers=HEADERS).text


def _post_json(url: str, body: dict, timeout: int = 15):
    resp = requests.post(url, json=body, headers={**JSON_HEADERS, "Content-Type": "application/json"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _search(query: str, region: str = "wt-wt", max_results: int = 20, timelimit: str | None = "m") -> list[dict]:
    """Web search results ({title, href, body}); several engines are tried in turn."""
    from .web import _ddgs_text
    return _ddgs_text(query, max_results, region, timelimit)


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _cached(key, ttl: float, fn):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)
    return value


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _try(fn, item):
    try:
        return fn(item)
    except Exception:
        return None


def pmap(fn, items, workers: int = 16, budget: float | None = None) -> list:
    """fn over items in parallel. A failure, or anything still running after `budget` seconds, gives None
    (it never holds up the answer)."""
    items = list(items)
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
    futures = [pool.submit(_try, fn, item) for item in items]
    done, _ = wait(futures, timeout=budget)
    pool.shutdown(wait=False, cancel_futures=True)
    return [f.result() if f in done else None for f in futures]


def _flat(results) -> list[dict]:
    return [row for rows in results if rows for row in rows]


def _title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug or "").strip().title()


def _date_words(text: str) -> str:
    """'October  5, 2026' -> '2026-10-05'."""
    try:
        return datetime.strptime(re.sub(r"\s+", " ", (text or "").strip()), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def roles_of(query: str, n: int = 2) -> list[str]:
    """The distinct roles in a query, as search words: 'associate product manager, product analyst' ->
    ['product manager', 'product analyst'] (a narrower version of a role is covered by the wider search)."""
    sets: list[list[str]] = []
    for part in re.split(r"[,;/|]|\bor\b", query or ""):
        words = J.terms(part)
        if words and words not in sets:
            sets.append(words)
    keep = [w for w in sets if not any(set(o) < set(w) for o in sets)]
    out = [" ".join(w) for w in keep[:n]]
    return out or ([query.strip()] if (query or "").strip() else [])


# ------------------------------------------------------------------ places (any country)
def _c(iso2: str, iso3: str, aka: str, cities: str = "") -> dict:
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()]  # noqa: E731
    return {"iso2": iso2, "iso3": iso3, "aka": split(aka), "cities": split(cities)}


COUNTRIES = {
    "india": _c("in", "IND", "india, bharat", "bengaluru, bangalore, hyderabad, mumbai, pune, delhi, new delhi, gurugram, "
                "gurgaon, noida, chennai, kolkata, ahmedabad, jaipur, kochi, trivandrum, thiruvananthapuram, coimbatore, "
                "indore, chandigarh, mysuru, mysore, visakhapatnam, vizag, bhubaneswar, lucknow, nagpur, vadodara, mohali"),
    "united states": _c("us", "USA", "united states, united states of america, usa, us, u.s., u.s.a., america",
                        "san francisco, new york, nyc, seattle, austin, boston, chicago, los angeles, denver, atlanta, "
                        "miami, dallas, houston, san jose, palo alto, mountain view, sunnyvale, menlo park, santa clara, "
                        "redmond, washington dc, philadelphia, phoenix, san diego, pittsburgh, minneapolis, nashville, "
                        "raleigh, salt lake city, california, texas, florida, virginia, massachusetts, colorado, illinois, "
                        "north carolina, pennsylvania, new jersey, arizona, oregon, minnesota, michigan, ohio"),
    "united kingdom": _c("gb", "GBR", "united kingdom, uk, u.k., great britain, britain, england, scotland, wales",
                         "london, manchester, edinburgh, glasgow, bristol, oxford, leeds, belfast, brighton"),
    "canada": _c("ca", "CAN", "canada", "toronto, vancouver, montreal, ottawa, calgary, waterloo"),
    "germany": _c("de", "DEU", "germany, deutschland", "berlin, munich, münchen, hamburg, frankfurt, cologne, köln, stuttgart, düsseldorf"),
    "france": _c("fr", "FRA", "france", "paris, lyon, toulouse, nantes"),
    "netherlands": _c("nl", "NLD", "netherlands, holland", "amsterdam, rotterdam, utrecht, eindhoven, the hague"),
    "ireland": _c("ie", "IRL", "ireland", "dublin, cork"),
    "spain": _c("es", "ESP", "spain", "madrid, barcelona"),
    "portugal": _c("pt", "PRT", "portugal", "lisbon, porto"),
    "italy": _c("it", "ITA", "italy", "milan, rome"),
    "poland": _c("pl", "POL", "poland", "warsaw, krakow, kraków, wroclaw"),
    "sweden": _c("se", "SWE", "sweden", "stockholm, gothenburg"),
    "denmark": _c("dk", "DNK", "denmark", "copenhagen"),
    "norway": _c("no", "NOR", "norway", "oslo"),
    "finland": _c("fi", "FIN", "finland", "helsinki"),
    "switzerland": _c("ch", "CHE", "switzerland", "zurich, zürich, geneva, lausanne"),
    "austria": _c("at", "AUT", "austria", "vienna"),
    "belgium": _c("be", "BEL", "belgium", "brussels"),
    "czechia": _c("cz", "CZE", "czechia, czech republic", "prague, brno"),
    "romania": _c("ro", "ROU", "romania", "bucharest, cluj"),
    "estonia": _c("ee", "EST", "estonia", "tallinn"),
    "ukraine": _c("ua", "UKR", "ukraine", "kyiv, kiev, lviv"),
    "greece": _c("gr", "GRC", "greece", "athens"),
    "israel": _c("il", "ISR", "israel", "tel aviv, jerusalem, haifa"),
    "united arab emirates": _c("ae", "ARE", "united arab emirates, uae", "dubai, abu dhabi"),
    "saudi arabia": _c("sa", "SAU", "saudi arabia, ksa", "riyadh, jeddah"),
    "singapore": _c("sg", "SGP", "singapore"),
    "malaysia": _c("my", "MYS", "malaysia", "kuala lumpur"),
    "indonesia": _c("id", "IDN", "indonesia", "jakarta"),
    "philippines": _c("ph", "PHL", "philippines", "manila"),
    "vietnam": _c("vn", "VNM", "vietnam", "ho chi minh city, hanoi"),
    "thailand": _c("th", "THA", "thailand", "bangkok"),
    "japan": _c("jp", "JPN", "japan", "tokyo, osaka"),
    "south korea": _c("kr", "KOR", "south korea, korea", "seoul"),
    "china": _c("cn", "CHN", "china", "beijing, shanghai, shenzhen, hangzhou"),
    "hong kong": _c("hk", "HKG", "hong kong"),
    "taiwan": _c("tw", "TWN", "taiwan", "taipei"),
    "australia": _c("au", "AUS", "australia", "sydney, melbourne, brisbane"),
    "new zealand": _c("nz", "NZL", "new zealand", "auckland, wellington"),
    "brazil": _c("br", "BRA", "brazil, brasil", "são paulo, sao paulo, rio de janeiro"),
    "mexico": _c("mx", "MEX", "mexico", "mexico city, guadalajara, monterrey"),
    "argentina": _c("ar", "ARG", "argentina", "buenos aires"),
    "colombia": _c("co", "COL", "colombia", "bogota, bogotá, medellin"),
    "chile": _c("cl", "CHL", "chile"),
    "south africa": _c("za", "ZAF", "south africa", "cape town, johannesburg"),
    "nigeria": _c("ng", "NGA", "nigeria", "lagos"),
    "kenya": _c("ke", "KEN", "kenya", "nairobi"),
    "egypt": _c("eg", "EGY", "egypt", "cairo"),
    "turkey": _c("tr", "TUR", "turkey, türkiye", "istanbul"),
    "pakistan": _c("pk", "PAK", "pakistan", "karachi, lahore, islamabad"),
    "bangladesh": _c("bd", "BGD", "bangladesh", "dhaka"),
    "sri lanka": _c("lk", "LKA", "sri lanka", "colombo"),
}
EUROPE = ["united kingdom", "germany", "france", "netherlands", "ireland", "spain", "portugal", "italy", "poland",
          "sweden", "denmark", "norway", "finland", "switzerland", "austria", "belgium", "czechia", "romania", "estonia",
          "ukraine", "greece"]
REGIONS = {"europe": EUROPE, "eu": EUROPE, "emea": EUROPE + ["israel", "united arab emirates", "saudi arabia", "egypt",
                                                             "south africa", "nigeria", "kenya", "turkey"],
           "middle east": ["united arab emirates", "saudi arabia", "israel", "turkey", "egypt"],
           "apac": ["india", "singapore", "malaysia", "indonesia", "philippines", "vietnam", "thailand", "japan",
                    "south korea", "china", "hong kong", "taiwan", "australia", "new zealand"],
           "latam": ["brazil", "mexico", "argentina", "colombia", "chile"],
           "north america": ["united states", "canada"]}
CITY_SYNONYMS = [{"bengaluru", "bangalore"}, {"gurugram", "gurgaon"}, {"mumbai", "bombay"}, {"chennai", "madras"},
                 {"kolkata", "calcutta"}, {"thiruvananthapuram", "trivandrum"}, {"mysuru", "mysore"},
                 {"visakhapatnam", "vizag"}, {"delhi", "new delhi"}, {"new york", "nyc"}, {"munich", "münchen"},
                 {"cologne", "köln"}, {"zurich", "zürich"}, {"krakow", "kraków"}, {"kyiv", "kiev"},
                 {"sao paulo", "são paulo"}, {"bogota", "bogotá"}]
REMOTE_WORDS = {"remote", "anywhere", "worldwide", "global", "work from home", "wfh"}


def _key(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def place_key(place: str) -> str | None:
    """The country a place is in: 'Hyderabad' -> 'india', 'US' -> 'united states'."""
    p = _key(place)
    if p in COUNTRIES:
        return p
    for key, c in COUNTRIES.items():
        if p in c["aka"] or p in c["cities"]:
            return key
    return None


def is_city(place: str) -> bool:
    p = _key(place)
    key = place_key(p)
    return bool(key) and p != key and p not in COUNTRIES[key]["aka"]


def region_countries(place: str) -> list[str]:
    return REGIONS.get(_key(place), [])


def city_names(city: str) -> set[str]:
    c = _key(city)
    for group in CITY_SYNONYMS:
        if c in group:
            return set(group)
    return {c}


@lru_cache(maxsize=512)
def place_aliases(place: str) -> tuple[str, ...]:
    """Words that mean this place in a job's location: a country with its other names and main cities; a city with
    its country (a job elsewhere in the same country still fits, as before); a region with all its countries."""
    p = _key(place)
    if not p:
        return ()
    keys = region_countries(p) or ([place_key(p)] if place_key(p) else [])
    out = [p, *city_names(p)]
    for k in keys:
        c = COUNTRIES[k]
        out += [k, *c["aka"], *c["cities"]]
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=4096)
def _place_rx(alias: str):
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


def has_place(text: str, alias: str) -> bool:
    alias = (alias or "").strip().lower()
    return bool(alias) and _place_rx(alias).search((text or "").lower()) is not None


def named_places(locations) -> list[str]:
    return [str(l).strip() for l in locations or [] if _key(l) and _key(l) not in REMOTE_WORDS]


def api_places(locations, n: int = 2) -> list[tuple[str, str | None]]:
    """[(label, country key)] for APIs that take a place: named places only (not 'Remote'), at most n."""
    return [(p, place_key(p)) for p in named_places(locations)][:n]


def wanted_countries(locations) -> set[str]:
    keys = set()
    for p in named_places(locations):
        keys |= set(region_countries(p))
        if place_key(p):
            keys.add(place_key(p))
    return keys


# ------------------------------------------------------------------ company types
SERVICE_RE = re.compile(
    r"\b(accenture|deloitte|pwc|pricewaterhouse|kpmg|ernst\s*&?\s*young|\bey\b|capgemini|cognizant|infosys|tcs|"
    r"tata consultancy|wipro|hcl(?:tech)?|tech mahindra|ltimindtree|mindtree|l&t technology|ltts|mphasis|"
    r"persistent systems|zensar|hexaware|birlasoft|coforge|cyient|sonata software|mastek|nagarro|epam|globant|"
    r"thoughtworks|endava|luxoft|ibm consulting|dxc|ntt data|kyndryl|genpact|\bexl\b|wns|publicis sapient|virtusa|"
    r"\bust\b|ust global|quest global|happiest minds|sasken|kpit|tavant|ciklum|softserve|atos|sopra steria|\bcgi\b|"
    r"unisys|tata elxsi|mckinsey|boston consulting|\bbcg\b|bain & company|gartner|it services|consultancy services|"
    r"software services|technology services|digital services|solutions pvt)\b", re.I)
STAFFING_RE = re.compile(
    r"\b(staffing|recruit(?:ment|ers?|ing)|manpower|placements?|talent (?:solutions|acquisition|hub)|hr solutions|"
    r"hr services|headhunt\w*|teamlease|randstad|adecco|quess|xpheno|ciel hr|careernet|michael page|\bhays\b|"
    r"kelly services|robert half|allegis|hiring partner|executive search|consultants? (?:pvt|private))\b", re.I)
_STARTUP_STAGES = re.compile(r"seed|series [a-c]|early|angel|bootstrap|startup", re.I)


def company_type(job: dict) -> str | None:
    """product / service / enterprise / startup / staffing, or None when unknown."""
    if job.get("ctype"):
        return job["ctype"]
    name = job.get("company") or ""
    if STAFFING_RE.search(name):
        return "staffing"
    if SERVICE_RE.search(name):
        return "service"
    if job.get("source") in ("YC", "HN Who's Hiring") or job.get("funding"):
        return "startup"
    return None


def type_ok(ctype: str | None, wanted) -> bool:
    """Does a company type pass the user's filter? 'product' also takes startups and companies whose type isn't known
    (career boards on Greenhouse/Lever/Ashby are nearly all product companies)."""
    wanted = {w.lower() for w in wanted or [] if w}
    if not wanted or "any" in wanted:
        return True
    if "product" in wanted and ctype in (None, "product", "startup"):
        return True
    return ctype in wanted


# ------------------------------------------------------------------ Workday
# (company, tenant, wdN, site, type) - every one verified to answer the job-search API.
WORKDAY = [
    ("Salesforce", "salesforce", "wd12", "External_Career_Site", "product"),
    ("Adobe", "adobe", "wd5", "external_experienced", "product"),
    ("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite", "product"),
    ("Intel", "intel", "wd1", "External", "product"),
    ("Mastercard", "mastercard", "wd1", "CorporateCareers", "product"),
    ("PayPal", "paypal", "wd1", "jobs", "product"),
    ("Workday", "workday", "wd5", "Workday", "product"),
    ("Autodesk", "autodesk", "wd1", "Ext", "product"),
    ("HP", "hp", "wd5", "ExternalCareerSite", "product"),
    ("HPE", "hpe", "wd5", "Jobsathpe", "product"),
    ("Cisco", "cisco", "wd5", "Cisco_Careers", "product"),
    ("Broadcom", "broadcom", "wd1", "External_Career", "product"),
    ("Red Hat", "redhat", "wd5", "jobs", "product"),
    ("CrowdStrike", "crowdstrike", "wd5", "crowdstrikecareers", "product"),
    ("Zoom", "zoom", "wd5", "Zoom", "product"),
    ("eBay", "ebay", "wd5", "apply", "product"),
    ("Yahoo", "ouryahoo", "wd5", "careers", "product"),
    ("Expedia", "expedia", "wd108", "search", "product"),
    ("Samsung", "sec", "wd3", "Samsung_Careers", "product"),
    ("Sony", "sonyglobal", "wd1", "SonyGlobalCareers", "product"),
    ("Philips", "philips", "wd3", "jobs-and-careers", "product"),
    ("Thomson Reuters", "thomsonreuters", "wd5", "External_Career_Site", "product"),
    ("Motorola Solutions", "motorolasolutions", "wd5", "Careers", "product"),
    ("KLA", "kla", "wd1", "Search", "product"),
    ("Cadence", "cadence", "wd1", "External_Careers", "product"),
    ("Analog Devices", "analogdevices", "wd1", "External", "product"),
    ("Micron", "micron", "wd1", "External", "product"),
    ("Equinix", "equinix", "wd1", "External", "product"),
    ("Sprinklr", "sprinklr", "wd1", "careers", "product"),
    ("FIS", "fis", "wd5", "SearchJobs", "product"),
    ("Walmart Global Tech", "walmart", "wd504", "WalmartExternal", "enterprise"),
    ("Target", "target", "wd5", "targetcareers", "enterprise"),
    ("Nike", "nike", "wd1", "nke", "enterprise"),
    ("Disney", "disney", "wd5", "disneycareer", "enterprise"),
    ("Warner Bros. Discovery", "warnerbros", "wd5", "global", "enterprise"),
    ("CVS Health", "cvshealth", "wd1", "CVS_Health_Careers", "enterprise"),
    ("Capital One", "capitalone", "wd12", "Capital_One", "enterprise"),
    ("Citi", "citi", "wd5", "2", "enterprise"),
    ("Barclays", "barclays", "wd3", "External_Career_Site_Barclays", "enterprise"),
    ("Wells Fargo", "wf", "wd1", "WellsFargoJobs", "enterprise"),
    ("Fidelity", "fmr", "wd1", "targeted", "enterprise"),
    ("State Street", "statestreet", "wd1", "Global", "enterprise"),
    ("Northern Trust", "ntrs", "wd1", "northerntrust", "enterprise"),
    ("Synchrony", "synchronyfinancial", "wd5", "careers", "enterprise"),
    ("LSEG", "lseg", "wd3", "Careers", "enterprise"),
    ("Deutsche Bank", "db", "wd3", "DBWebsite", "enterprise"),
    ("ING", "ing", "wd3", "ICSGBLCOR", "enterprise"),
    ("Travelers", "travelers", "wd5", "External", "enterprise"),
    ("Pfizer", "pfizer", "wd1", "PfizerCareers", "enterprise"),
    ("AstraZeneca", "astrazeneca", "wd3", "Careers", "enterprise"),
    ("Novartis", "novartis", "wd3", "Novartis_Careers", "enterprise"),
    ("GE Vernova", "gevernova", "wd5", "Vernova_ExternalSite", "enterprise"),
    ("Unilever", "unilever", "wd3", "Unilever_Experienced_Professionals", "enterprise"),
    ("3M", "3m", "wd1", "Search", "enterprise"),
    ("Chevron", "chevron", "wd5", "jobs", "enterprise"),
    ("Shell", "shell", "wd3", "ShellCareers", "enterprise"),
    ("Maersk", "maersk", "wd3", "Maersk_Careers", "enterprise"),
    ("Accenture", "accenture", "wd103", "AccentureCareers", "service"),
    ("PwC", "pwc", "wd3", "Global_Experienced_Careers", "service"),
    ("Kyndryl", "kyndryl", "wd5", "KyndrylProfessionalCareers", "service"),
    ("DXC Technology", "dxctechnology", "wd1", "DXCJobs", "service"),
    ("Gartner", "gartner", "wd5", "EXT", "service"),
]
WORKDAY_HOW = ("Workday: open the apply link, choose 'Apply Manually' (or 'Autofill with Resume' with the tailored PDF), "
               "then sign in or 'Create Account' - list_accounts / the user's primary login, or vault_new_password for a "
               "new one; if Workday emails a verification link, read_emails and open it. Fill each step (My Information, "
               "My Experience, Application Questions, Voluntary Disclosures, Self Identify), Review, then Submit (the "
               "user approves). Every company's Workday is a separate account.")
_POSTED_ON = re.compile(r"posted\s+(today|yesterday|(\d+)(\+?)\s+days?\s+ago)", re.I)


def _wd_posted(text: str) -> str:
    m = _POSTED_ON.search(text or "")
    if not m:
        return ""
    word = m.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2)) + (1 if m.group(3) else 0)
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _walk_facets(facets):
    for f in facets or []:
        param = f.get("facetParameter")
        for v in f.get("values") or []:
            if "facetParameter" in v:
                yield from _walk_facets([v])
            elif v.get("id") and v.get("descriptor"):
                yield param, v["descriptor"], v["id"]


def _wd_location_facets(facets, places: list[str]):
    """Workday's own filter for the user's places: (facet parameter, [ids], label); {} when the site has no place
    filter (filter locally); None when it has one but none of the places has jobs for this search.
    Only ONE parameter is used, because Workday combines different parameters with AND."""
    leaves = [(p, d, i) for p, d, i in _walk_facets(facets)
              if p and re.search(r"country|location", p, re.I)
              and not re.search(r"region|state|province|distance|remote|main|group|type", p, re.I)]
    if not leaves:
        return {}
    countries, cities = [], set()
    for place in places:
        countries += region_countries(place)
        if place_key(place):
            countries.append(place_key(place))
        if is_city(place):
            cities |= city_names(place)
    country_leaves = [x for x in leaves if "country" in x[0].lower()]
    city_leaves = [x for x in leaves if "country" not in x[0].lower()]

    def names(key):
        return [key, *COUNTRIES[key]["aka"]]

    def pick(cands):
        by: dict[str, list] = {}
        for p, d, i in cands:
            by.setdefault(p, []).append((d, i))
        if not by:
            return None
        param = max(by, key=lambda k: len(by[k]))
        return param, [i for _, i in by[param]], ", ".join(dict.fromkeys(d for d, _ in by[param]))[:90]

    if cities:
        hit = pick([x for x in city_leaves if any(has_place(x[1], c) for c in cities)])
        if hit:
            return hit
    if countries:
        hit = pick([x for x in country_leaves if any(has_place(x[1], n) for k in countries for n in names(k))])
        if hit:
            return hit
        hit = pick([x for x in city_leaves
                    if any(has_place(x[1], n) for k in countries for n in names(k) + COUNTRIES[k]["cities"])])
        if hit:
            return hit
    return None


_GENERIC_ROLE_WORDS = {"manager", "management", "lead", "senior", "junior", "associate", "staff", "principal", "head",
                       "specialist", "executive", "officer", "intern", "director", "member", "team", "level"}


def _wd_family_facets(facets, role: str):
    """Workday's job-family filter for the role (e.g. 'Product Management' for 'product manager'): (param, [ids]) or
    None. Words like 'manager' or 'senior' don't count - they're in every family."""
    keys = [t for t in J.terms(role) if t not in _GENERIC_ROLE_WORDS and len(t) >= 3]
    if not keys:
        return None
    by: dict[str, list] = {}
    for param, desc, fid in _walk_facets(facets):
        if not param or not re.search(r"jobfamily|job_family|jobcategory|job_category|family|category", param, re.I):
            continue
        low = desc.lower()
        if any(k[:5] in low for k in keys):
            by.setdefault(param, []).append(fid)
    if not by:
        return None
    param = max(by, key=lambda k: len(by[k]))
    return param, by[param]


def _wd_search(company: str, tenant: str, wd: str, site: str, ctype: str | None, role: str, places: list[str],
               pages: int = 3) -> list[dict]:
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": role or ""}
    data = _post_json(api, body)
    label = ""
    applied = {}
    named = named_places(places)
    if named:
        chosen = _wd_location_facets(data.get("facets"), named)
        if chosen is None:
            return []                                   # nothing for this role in those places
        if chosen:
            param, ids, label = chosen
            applied[param] = ids
    family = _wd_family_facets(data.get("facets"), role) if role else None
    if family and family[0] not in applied:
        applied[family[0]] = family[1]
    if applied:
        body["appliedFacets"] = applied
        data = _post_json(api, body)
    postings = list(data.get("jobPostings") or [])
    total = data.get("total") or 0
    page = 1
    while page < pages and len(postings) < min(total, 20 * pages) and len(postings) >= 20 * page and total <= 60:
        body["offset"] = 20 * page
        postings += _post_json(api, body).get("jobPostings") or []
        page += 1
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    rows = []
    for p in postings:
        path = p.get("externalPath") or ""
        if not p.get("title") or not path:
            continue
        where = p.get("locationsText") or ""
        if label and (not where or re.fullmatch(r"\d+\s+locations?", where.strip(), re.I)):
            where = (where + " incl. " if where else "") + label
        rows.append({"source": f"{company} careers (Workday)", "title": p["title"], "company": company,
                     "location": where, "posted": _wd_posted(p.get("postedOn")), "url": base + path,
                     "ats": "workday", "direct": True, "ctype": ctype})
    return rows


def _workday_tenants(ctypes=None) -> list[tuple]:
    out = [t for t in WORKDAY if type_ok(t[4], ctypes)]
    seen = {(t[1], t[3]) for t in WORKDAY}
    for e in learned_of("workday"):
        if (e.get("tenant"), e.get("site")) not in seen and type_ok(e.get("ctype"), ctypes):
            out.append((e.get("company") or _title(e["tenant"]), e["tenant"], e["wd"], e["site"], e.get("ctype")))
    return out


def src_workday(query: str, locations: list[str], ctypes=None) -> list[dict]:
    """Every Workday careers site Karya knows (curated + learned), searched for the role in the user's places."""
    roles = roles_of(query)
    places = list(locations or [])

    def one(task):
        t, role = task
        return _cached(("wd", t[1], t[3], role, tuple(places)), 3600, lambda: _wd_search(*t, role, places))

    tasks = [(t, r) for t in _workday_tenants(ctypes) for r in roles]
    return [dict(row) for row in _flat(pmap(one, tasks, workers=40, budget=30))]


# ------------------------------------------------------------------ SmartRecruiters
SMARTRECRUITERS = [
    ("Bosch", "BoschGroup", "enterprise"), ("Freshworks", "Freshworks", "product"), ("ServiceNow", "ServiceNow", "product"),
    ("Ubisoft", "Ubisoft2", "product"), ("Deloitte", "Deloitte6", "service"), ("PhonePe", "PHONEPELIMITED", "product"),
    ("Wise", "Wise", "product"), ("Experian", "Experian", "product"), ("Canva", "Canva", "product"),
    ("NielsenIQ", "NielsenIQ", "product"), ("Avery Dennison", "AveryDennison", "enterprise"),
    ("Gameloft", "Gameloft", "product"), ("Endava", "Endava", "service"), ("Continental", "Continental", "enterprise"),
    ("Delivery Hero", "DeliveryHero", "product"), ("Grab", "Grab", "product"), ("Swiggy", "Swiggy", "product"),
    ("Unacademy", "Unacademy", "product"), ("Cars24", "Cars24", "product"),
]


def _sr_search(company: str, cid: str, ctype: str | None, role: str, iso2: str | None = None) -> list[dict]:
    params = {"q": role or "", "limit": 100}
    if iso2:
        params["country"] = iso2
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings", params)
    rows = []
    for p in data.get("content") or []:
        loc = p.get("location") or {}
        where = re.sub(r"(?:,\s*)+", ", ", loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), (loc.get("country") or "").upper()) if x)).strip(", ")
        if loc.get("remote"):
            where += " (remote)"
        name = (p.get("company") or {}).get("name") or company
        rows.append({"source": f"{company} careers (SmartRecruiters)", "title": p.get("name"),
                     "company": company if company and name.isupper() else name, "location": where,
                     "posted": p.get("releasedDate"), "url": f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}",
                     "experience": (p.get("experienceLevel") or {}).get("label"), "ats": "smartrecruiters",
                     "direct": True, "ctype": ctype})
    return rows


def _sr_boards(ctypes=None) -> list[tuple]:
    out = [b for b in SMARTRECRUITERS if type_ok(b[2], ctypes)]
    known = {b[1].lower() for b in SMARTRECRUITERS}
    for e in learned_of("smartrecruiters"):
        if e["slug"].lower() not in known and type_ok(e.get("ctype"), ctypes):
            out.append((e.get("company") or _title(e["slug"]), e["slug"], e.get("ctype")))
    return out


def src_smartrecruiters(query: str, locations: list[str], ctypes=None) -> list[dict]:
    roles = roles_of(query)
    codes = list(dict.fromkeys(COUNTRIES[k]["iso2"] for _, k in api_places(locations, 3) if k))[:2] or [None]

    def one(task):
        b, role, code = task
        return _cached(("sr", b[1], role, code), 3600, lambda: _sr_search(*b, role, code))

    tasks = [(b, r, c) for b in _sr_boards(ctypes) for r in roles for c in codes]
    return [dict(row) for row in _flat(pmap(one, tasks, workers=16, budget=25))]


# ------------------------------------------------------------------ big tech career APIs
def src_amazon(role: str, places, level=None) -> list[dict]:
    rows = []
    for label, key in places or [(None, None)]:
        params = {"base_query": role, "result_limit": 50, "sort": "recent", "offset": 0}
        if label:
            params["loc_query"] = label
        if key:
            params["country"] = COUNTRIES[key]["iso3"]
        for j in _get_json("https://www.amazon.jobs/en/search.json", params, timeout=20).get("jobs") or []:
            text = " ".join(j.get(k) or "" for k in ("description_short", "basic_qualifications", "preferred_qualifications"))
            rows.append({"source": "Amazon careers", "title": (j.get("title") or "").strip(), "company": "Amazon",
                         "location": j.get("location") or "", "posted": _date_words(j.get("posted_date")),
                         "url": "https://www.amazon.jobs" + (j.get("job_path") or ""), "ats": "amazon",
                         "direct": True, "ctype": "product", "text": J.job_text(J._clean_html(text))})
    return rows


def src_microsoft(role: str, places, level=None) -> list[dict]:
    rows = []
    for label, _ in places or [(None, None)]:
        for start in (0, 10):
            data = _get_json("https://apply.careers.microsoft.com/api/pcsx/search",
                             {"domain": "microsoft.com", "query": role, "location": label or "", "start": start,
                              "num": 10}, timeout=20)
            positions = (data.get("data") or {}).get("positions") or []
            for p in positions:
                rows.append({"source": "Microsoft careers", "title": p.get("name"), "company": "Microsoft",
                             "location": "; ".join((p.get("locations") or [])[:3]), "posted": p.get("postedTs"),
                             "url": f"https://apply.careers.microsoft.com/careers/job/{p.get('id')}",
                             "ats": "microsoft", "direct": True, "ctype": "product"})
            if len(positions) < 10:
                break
    return rows


_GOOGLE_DATA = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(\[.*?\]), sideChannel", re.S)


def src_google(role: str, places, level=None) -> list[dict]:
    rows = []
    for label, _ in places or [(None, None)]:
        params = {"q": role}
        if label:
            params["location"] = label
        if level in ("entry", "associate"):
            params["target_level"] = "EARLY"
        elif level == "internship":
            params["target_level"] = "INTERN_AND_APPRENTICE"
        html = _get_text("https://www.google.com/about/careers/applications/jobs/results/", params)
        m = _GOOGLE_DATA.search(html)
        if not m:
            continue
        data = json.loads(m.group(1))
        slugs = dict(re.findall(r'href="jobs/results/(\d+)-([a-z0-9-]+)', html))
        for job in (data[0] if data and isinstance(data[0], list) else []):
            try:
                jid, title = str(job[0]), job[1]
                where = "; ".join(loc[0] for loc in job[9] or [] if loc)
                text = " ".join((part or [None, ""])[1] or "" for part in (job[3], job[4]))
                slug = slugs.get(jid) or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                rows.append({"source": "Google careers", "title": title, "company": job[7] or "Google",
                             "location": where, "posted": (job[12] or [None])[0],
                             "url": f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}",
                             "ats": "google", "direct": True, "ctype": "product",
                             "text": J.job_text(J._clean_html(text))})
            except (IndexError, TypeError, AttributeError):
                continue
    return rows


def src_netflix(role: str, places, level=None) -> list[dict]:
    rows = []
    for label, _ in places or [(None, None)]:
        data = _get_json("https://explore.jobs.netflix.net/api/apply/v2/jobs",
                         {"domain": "netflix.com", "query": role, "location": label or "", "start": 0, "num": 20})
        for p in data.get("positions") or []:
            rows.append({"source": "Netflix careers", "title": p.get("name"), "company": "Netflix",
                         "location": p.get("location") or "; ".join(p.get("locations") or []),
                         "posted": p.get("t_create"),
                         "url": p.get("canonicalPositionUrl") or f"https://explore.jobs.netflix.net/careers/job/{p.get('id')}",
                         "ats": "eightfold", "direct": True, "ctype": "product",
                         "text": J.job_text(J._clean_html(p.get("job_description") or ""))})
    return rows


def _atlassian_listings() -> list[dict]:
    return _cached(("atlassian",), 3600, lambda: _get_json("https://www.atlassian.com/endpoint/careers/listings", timeout=20))


def src_atlassian(role: str, places, level=None) -> list[dict]:
    rows = []
    for x in _atlassian_listings() or []:
        text = " ".join(x.get(k) or "" for k in ("overview", "responsibilities", "qualifications"))
        rows.append({"source": "Atlassian careers", "title": x.get("title"), "company": "Atlassian",
                     "location": "; ".join(re.sub(r"\s+", " ", l) for l in (x.get("locations") or [])[:3]),
                     "posted": ((x.get("portalJobPost") or {}).get("updatedDate") or "")[:10],
                     "url": f"https://www.atlassian.com/company/careers/details/{x.get('id')}",
                     "apply": x.get("applyUrl") or (x.get("portalJobPost") or {}).get("portalUrl"),
                     "ats": "atlassian", "direct": True, "ctype": "product",
                     "text": J.job_text(J._clean_html(text))})
    return rows


BIG_TECH = (src_amazon, src_microsoft, src_google, src_netflix, src_atlassian)


def src_bigtech(query: str, locations: list[str], level=None, ctypes=None) -> list[dict]:
    """Amazon, Microsoft, Google, Netflix and Atlassian's own job search."""
    if not type_ok("product", ctypes):
        return []
    role = (roles_of(query, 1) or [query])[0]
    places = api_places(locations, 2)
    return _flat(pmap(lambda fn: fn(role, places, level), BIG_TECH, workers=5, budget=30))


# ------------------------------------------------------------------ India boards
def _india_cities(locations) -> list[str] | None:
    """The Indian cities the user named ([] = anywhere in India), or None when the search isn't about India."""
    named = named_places(locations)
    if not named:
        return []
    indian = [p for p in named if place_key(p) == "india"]
    if not indian:
        return None
    return [] if any(not is_city(p) for p in indian) else indian[:2]


def _instahyre(role: str, city: str | None, level=None) -> list[dict]:
    params = {"skills": role}
    if city:
        params["location"] = city
    if level in ("internship", "entry"):
        params["years"] = 0
    rows = []
    for o in _get_json("https://www.instahyre.com/api/v1/job_search", params).get("objects") or []:
        emp = o.get("employer") or {}
        size = emp.get("employee_count")
        where = o.get("locations") or ""
        rows.append({"source": "Instahyre", "title": o.get("title") or o.get("candidate_title"),
                     "company": emp.get("company_name"), "location": (where + ", India") if where else "India",
                     "url": o.get("public_url"), "ats": "instahyre", "about": emp.get("company_tagline"),
                     "skills_text": " ".join(o.get("keywords") or []),
                     "ctype": "startup" if isinstance(size, int) and 0 < size <= 500 else None})
    return rows


_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def _exp_text(lo, hi) -> str:
    if lo is None and hi is None:
        return ""
    return f"{lo or 0}-{hi} years" if hi is not None else f"{lo}+ years"


def _cutshort(role: str, city: str | None = None, level=None) -> list[dict]:
    slug = "-".join(J.terms(role)) or "software-engineer"
    m = _NEXT_DATA.search(_get_text(f"https://cutshort.io/jobs/{slug}-jobs"))
    if not m:
        return []
    queries = json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
    query = next((q for q in queries if (q.get("queryKey") or [""])[0] == "jobListData"), None)
    jobs = (((query or {}).get("state") or {}).get("data") or {}).get("data", {}).get("pageData", {}).get("jobs") or []
    rows = []
    for x in jobs:
        cd = x.get("companyDetails") or {}
        kind = str(cd.get("type") or "").lower()
        ctype = "service" if "service" in kind else "product" if "product" in kind else None
        if ctype != "service" and _STARTUP_STAGES.search(str(cd.get("stage") or "")):
            ctype = "startup"
        if x.get("hiringForClient"):
            ctype = "staffing"
        exp = x.get("expRange") or {}
        exp_text = _exp_text(exp.get("min"), exp.get("max")) if isinstance(exp, dict) else ""
        where = x.get("locationsText") or ", ".join(str(l) for l in x.get("locations") or [])
        if x.get("remoteType") and "remote" in str(x.get("remoteType")).lower():
            where += " (remote)"
        rows.append({"source": "Cutshort", "title": x.get("headline"), "company": cd.get("name"),
                     "location": (where + ", India") if where and "india" not in where.lower() else (where or "India"),
                     "url": x.get("publicUrl"), "salary": x.get("salaryRangeText"), "experience": exp_text,
                     "text": f"Experience: {exp_text}." if exp_text else "", "ats": "cutshort", "ctype": ctype,
                     "skills_text": " ".join(str(s) for s in x.get("allSkills") or [])})
    return rows


def _foundit(role: str, city: str | None = None, level=None) -> list[dict]:
    params = {"query": role, "limit": 40, "start": 0, "locations": city or "India"}
    rows = []
    for x in _get_json("https://www.foundit.in/home/api/searchResultsPage", params).get("data") or []:
        cities = ", ".join(l.get("city") for l in x.get("locations") or [] if l.get("city"))
        lo = (x.get("minimumExperience") or {}).get("years")
        hi = (x.get("maximumExperience") or {}).get("years")
        exp = _exp_text(lo, hi)
        apply = x.get("applyUrl") or x.get("redirectUrl") or ""
        page = ("https://www.foundit.in" + x["jdUrl"]) if x.get("jdUrl") else apply
        rows.append({"source": "foundit", "title": x.get("title"), "company": x.get("companyName"),
                     "location": f"{cities}, India" if cities else "India", "posted": x.get("postedAt") or x.get("createdAt"),
                     "url": page, "apply": apply if apply and apply != page else None, "experience": exp,
                     "text": f"Experience: {exp}." if exp else "", "ats": "foundit",
                     "via_linkedin": "linkedin.com" in apply})
    return rows


def src_india(query: str, locations: list[str], level=None, ctypes=None) -> list[dict]:
    """Instahyre, Cutshort and foundit - only when the search is about India (or has no place)."""
    cities = _india_cities(locations)
    if cities is None:
        return []
    role = (roles_of(query, 1) or [query])[0]
    tasks = [(_instahyre, c) for c in (cities or [None])] + [(_cutshort, None)] + [(_foundit, c) for c in (cities or [None])]
    rows = _flat(pmap(lambda t: t[0](role, t[1], level), tasks, workers=6, budget=25))
    return [r for r in rows if type_ok(company_type(r), ctypes)]


# ------------------------------------------------------------------ US / Europe boards
MUSE_CATEGORIES = [(r"product", "Product Management"), (r"design|\bux\b|\bui\b", "Design and UX"),
                   (r"data scien|machine learning|\bml\b|\bai\b", "Data Science"),
                   (r"data|analyst|analytics", "Data and Analytics"),
                   (r"engineer|developer|software|frontend|backend|fullstack|devops|mobile|programmer", "Software Engineering"),
                   (r"marketing|growth|seo|content", "Marketing"), (r"sales|account executive|business development", "Sales"),
                   (r"project|program", "Project Management"), (r"support|customer", "Customer Service")]
MUSE_LEVELS = {"internship": "Internship", "entry": "Entry Level", "associate": "Entry Level", "mid": "Mid Level",
               "senior": "Senior Level"}


def _muse(query: str, level=None) -> list[dict]:
    low = J.norm(query)
    category = next((c for pat, c in MUSE_CATEGORIES if re.search(pat, low)), None)
    rows = []
    for page in (0, 1, 2):
        params = {"page": page}
        if category:
            params["category"] = category
        if MUSE_LEVELS.get(level or ""):
            params["level"] = MUSE_LEVELS[level]
        data = _get_json("https://www.themuse.com/api/public/jobs", params)
        for x in data.get("results") or []:
            rows.append({"source": "The Muse", "title": (x.get("name") or "").strip(),
                         "company": (x.get("company") or {}).get("name"),
                         "location": "; ".join(l.get("name", "") for l in x.get("locations") or []),
                         "posted": x.get("publication_date"), "url": (x.get("refs") or {}).get("landing_page"),
                         "experience": ", ".join(l.get("name", "") for l in x.get("levels") or []), "ats": "muse",
                         "text": J.job_text(J._clean_html(x.get("contents") or ""))})
        if page + 1 >= (data.get("page_count") or 0):
            break
    return rows


def _arbeitnow() -> list[dict]:
    def fetch():
        rows = []
        for page in (1, 2):
            for x in _get_json("https://www.arbeitnow.com/api/job-board-api", {"page": page}).get("data") or []:
                rows.append({"source": "Arbeitnow", "title": x.get("title"), "company": x.get("company_name"),
                             "location": (x.get("location") or "") + (" (remote)" if x.get("remote") else ""),
                             "posted": x.get("created_at"), "url": x.get("url"), "ats": "arbeitnow",
                             "skills_text": " ".join(x.get("tags") or []),
                             "text": J.job_text(J._clean_html(x.get("description") or ""))})
        return rows
    return [dict(r) for r in _cached(("arbeitnow",), 3600, fetch)]


def src_boards(query: str, locations: list[str], level=None, ctypes=None) -> list[dict]:
    """The Muse (mostly US) and Arbeitnow (Germany/Europe), when the search is about those places (or has none)."""
    keys = wanted_countries(locations)
    named = named_places(locations)
    tasks = []
    if not named or keys - {"india"}:
        tasks.append(lambda: _muse(query, level))
    if keys & set(EUROPE):
        tasks.append(_arbeitnow)
    rows = _flat(pmap(lambda fn: fn(), tasks, workers=2, budget=25))
    return [r for r in rows if type_ok(company_type(r), ctypes)]


# ------------------------------------------------------------------ learned boards (from search, funding, the user)
LEARNED_MAX = 300
_LEARN_LOCK = threading.Lock()


def _learned_path():
    return J.CACHE_DIR / "learned_boards.json"


def learned() -> dict:
    try:
        data = json.loads(_learned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def learned_of(ats: str) -> list[dict]:
    return [e for e in learned().values() if isinstance(e, dict) and e.get("ats") == ats]


def curated_keys() -> set[str]:
    keys = {f"{ats}:{slug}" for slug, ats in J.COMPANY_BOARDS.items()}
    keys |= {f"workday:{t}|{w}|{s}" for _, t, w, s, _ in WORKDAY}
    keys |= {f"smartrecruiters:{cid}" for _, cid, _ in SMARTRECRUITERS}
    return keys


def learn(info: dict, company: str = "", via: str = "search", **extra) -> None:
    """Remember a company's careers board so later searches include it."""
    key = info.get("key")
    if not key or not info.get("ats") or key in curated_keys():
        return
    with _LEARN_LOCK:
        data = learned()
        entry = data.get(key) or {"added": time.time(), "via": via}
        entry.update({k: v for k, v in info.items() if k not in ("job", "rows", "open_roles") and v not in (None, "")})
        if company:
            entry["company"] = company
        entry.update({k: v for k, v in extra.items() if v not in (None, "")})
        entry["seen"] = time.time()
        data[key] = entry
        if len(data) > LEARNED_MAX:
            data = dict(sorted(data.items(), key=lambda kv: -float(kv[1].get("seen", 0)))[:LEARNED_MAX])
        path = _learned_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ careers URLs of any company
ATS_URL = [
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"
                           r"(/job/[^?#\s\"'<>]+)?")),
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=|embed/job_app\?for=)?"
                              r"([A-Za-z0-9_-]+)(?:/jobs/(\d+))?")),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_.-]+)(?:/([0-9a-f-]{36}))?")),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)(?:/([0-9a-f-]{36}))?")),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)(?:/(\d{6,})[^?#\s\"'<>]*)?")),
    ("workable", re.compile(r"https?://apply\.workable\.com/([a-z0-9_-]+)(?:/j/([A-Za-z0-9]+))?")),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com(?:/o/([a-z0-9-]+))?")),
    ("jobvite", re.compile(r"https?://jobs\.jobvite\.com/([A-Za-z0-9_-]+)(?:/job/([A-Za-z0-9]+))?")),
]
_NOT_SLUGS = {"embed", "jobs", "api", "v1", "en", "careers", "search", "www", "job", "login", "signin", "j", "o", "wday",
              "__assets__", "assets", "static"}


def parse_ats_url(url: str) -> dict | None:
    """Which careers system a URL belongs to: {ats, slug | tenant/wd/site, job, key}, or None."""
    for ats, rx in ATS_URL:
        m = rx.match((url or "").strip())
        if not m:
            continue
        if ats == "workday":
            tenant, wd, site, path = m.groups()
            if site.lower() in _NOT_SLUGS:
                return None
            return {"ats": ats, "tenant": tenant, "wd": wd, "site": site, "job": path,
                    "key": f"workday:{tenant}|{wd}|{site}"}
        slug, job = m.group(1), m.group(2)
        if slug.lower() in _NOT_SLUGS:
            return None
        return {"ats": ats, "slug": slug, "job": job,
                "key": f"{ats}:{slug if ats == 'smartrecruiters' else slug.lower()}"}
    return None


def norm_name(text: str) -> str:
    low = (text or "").lower()
    low = re.sub(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|co|gmbh|plc|technologies|technology|labs|"
                 r"software|careers|jobs|group|holdings|hq|the)\b", " ", low)
    return re.sub(r"[^a-z0-9]", "", low)


def similar(a: str, b: str) -> bool:
    x, y = norm_name(a), norm_name(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = sorted((x, y), key=len)
    if len(short) >= 4 and long_.startswith(short):
        return True
    return SequenceMatcher(None, x, y).ratio() >= 0.85


def _recruitee(slug: str, company: str | None = None) -> list[dict]:
    name = company or _title(slug)
    rows = []
    for o in _get_json(f"https://{slug}.recruitee.com/api/offers/").get("offers") or []:
        where = o.get("location") or ", ".join(x for x in (o.get("city"), o.get("country")) if x)
        if o.get("remote"):
            where += " (remote)"
        rows.append({"source": f"{name} careers (Recruitee)", "title": o.get("title"),
                     "company": o.get("company_name") or name, "location": where,
                     "posted": o.get("published_at") or o.get("created_at"),
                     "url": o.get("careers_url") or f"https://{slug}.recruitee.com/o/{o.get('slug')}",
                     "apply": o.get("careers_apply_url"), "ats": "recruitee", "direct": True,
                     "text": J.job_text(J._clean_html((o.get("description") or "") + " " + (o.get("requirements") or "")))})
    return rows


def src_learned_other(query: str, locations: list[str], ctypes=None) -> list[dict]:
    """Boards Karya learned on careers systems without a search API (Recruitee, Jobvite): read in full, cached 3 h."""
    boards = [e for e in learned().values() if isinstance(e, dict) and e.get("ats") in ("recruitee", "jobvite")
              and e.get("slug") and type_ok(e.get("ctype"), ctypes)]
    boards = sorted(boards, key=lambda e: -float(e.get("seen", 0)))[:40]
    words = J.terms(query)
    rows = _flat(pmap(lambda e: _cached(("board", e["ats"], e["slug"]), 3 * 3600, lambda: fetch_board(e)), boards,
                      workers=12, budget=20))
    return [dict(r) for r in rows if not words or any(w in J.norm(r.get("title", "")) for w in words)]


_JOBVITE_ROW = re.compile(r'<td class="jv-job-list-name">\s*<a href="(/[^"]+/job/[A-Za-z0-9]+)"[^>]*>\s*([^<]+?)\s*</a>\s*</td>'
                          r'\s*<td class="jv-job-list-location">(.*?)</td>', re.S)


def _jobvite(slug: str, company: str | None = None) -> list[dict]:
    name = company or _title(slug)
    html = _get_text(f"https://jobs.jobvite.com/{slug}/jobs", timeout=15)
    rows = []
    for path, title, where in _JOBVITE_ROW.findall(html):
        place = re.sub(r"\s+", " ", J._clean_html(where)).replace(" ,", ",").strip()
        rows.append({"source": f"{name} careers (Jobvite)", "title": J._clean_html(title), "company": name,
                     "location": place, "url": "https://jobs.jobvite.com" + path, "ats": "jobvite", "direct": True})
    return rows


def fetch_board(info: dict, role: str = "", places=()) -> list[dict]:
    """The jobs on one company's board (all of them for Greenhouse/Lever/Ashby/Recruitee/Jobvite; a search for the
    others)."""
    ats = info.get("ats")
    if ats in ("greenhouse", "lever", "ashby"):
        return J._board(info["slug"], ats, info.get("company"))
    if ats == "workday":
        return _wd_search(info.get("company") or _title(info["tenant"]), info["tenant"], info["wd"], info["site"],
                          info.get("ctype"), role, list(places or []), pages=1)
    if ats == "smartrecruiters":
        return _sr_search(info.get("company") or _title(info["slug"]), info["slug"], info.get("ctype"), role)
    if ats == "recruitee":
        return _recruitee(info["slug"], info.get("company"))
    if ats == "jobvite":
        return _jobvite(info["slug"], info.get("company"))
    return []


def _split_title(title: str, company: str) -> tuple[str, str]:
    """A search result's page title -> (job title, company)."""
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(r"\s*[|\-–]\s*(careers?|jobs?|workday|lever|greenhouse|ashby|smartrecruiters)\s*$", "", t, flags=re.I)
    m = re.match(r"(?:job application for\s+)?(.+?)\s+at\s+(.+)$", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = [p.strip() for p in re.split(r"\s+[-|–@]\s+", t) if p.strip()]
    if len(parts) >= 2:
        for i, part in enumerate(parts):
            if similar(part, company):
                rest = parts[:i] + parts[i + 1:]
                return " - ".join(rest), part
        return parts[0], company
    return t, company


DISCOVER_SITES = [("workday", "site:myworkdayjobs.com"), ("greenhouse", "site:job-boards.greenhouse.io"),
                  ("greenhouse", "site:boards.greenhouse.io"), ("lever", "site:jobs.lever.co"),
                  ("ashby", "site:jobs.ashbyhq.com"), ("smartrecruiters", "site:jobs.smartrecruiters.com"),
                  ("workable", "site:apply.workable.com"), ("jobvite", "site:jobs.jobvite.com")]


def src_discover(query: str, locations: list[str], level=None, ctypes=None, max_boards: int = 12,
                 budget: float = 28) -> list[dict]:
    """Web search finds job pages for this role and place on the careers systems companies use, then Karya reads each
    new company's board through its API (more jobs, real locations and dates) and remembers the company."""
    started = time.time()
    role = (roles_of(query, 1) or [query])[0]
    place = (named_places(locations) or [""])[0]
    # Worldwide and without a time limit: with either, the engines return nothing for site: searches.
    queries = [f'{site} "{role}" {place}'.strip() for _, site in DISCOVER_SITES]

    def search(q):
        hits = _search(q, "wt-wt", 20, None)
        if hits:
            with _CACHE_LOCK:
                _CACHE[("search", q)] = (time.time(), hits)
        return hits

    def cached_or_search(q):
        with _CACHE_LOCK:
            hit = _CACHE.get(("search", q))
        return hit[1] if hit and time.time() - hit[0] < 6 * 3600 else search(q)

    results = pmap(cached_or_search, queries, workers=8, budget=budget * 0.6)
    boards: dict[str, dict] = {}
    found_rows: list[dict] = []
    # Boards the other sources already search (curated or learned): not fetched again, and their search hits aren't
    # listed twice (the board's own copy has the real location and date).
    covered = curated_keys() | {k for k, e in learned().items()
                                if isinstance(e, dict) and e.get("ats") in ("greenhouse", "lever", "ashby", "workday",
                                                                            "smartrecruiters")}
    for res in results:
        for r in res or []:
            url = (r.get("href") or r.get("url") or "").split("#")[0]
            info = parse_ats_url(url)
            if not info:
                continue
            boards.setdefault(info["key"], dict(info))
            if info.get("job") and info["key"] not in covered:
                company = _title(info.get("slug") or info.get("tenant") or "")
                title, company = _split_title(r.get("title") or "", company)
                found_rows.append({"source": f"{company} careers ({info['ats'].title()}, found by search)",
                                   "title": title, "company": company,
                                   "location": f"{place} (search match)" if place else "", "url": url,
                                   "ats": info["ats"], "direct": True, "text": (r.get("body") or "")[:400]})
    new = [b for k, b in boards.items() if k not in covered and b["ats"] != "workable"][:max_boards]
    left = max(4.0, budget - (time.time() - started))
    fetched = pmap(lambda b: fetch_board(b, role, locations), new, workers=8, budget=left)
    rows: list[dict] = []
    for board, got in zip(new, fetched):
        if got:
            learn(board, company=got[0].get("company") or "", via="search", role=role)
            rows += got
    have = {_job_key(r.get("url")) for r in rows}
    rows += [r for r in found_rows if _job_key(r["url"]) not in have]
    return [r for r in rows if type_ok(company_type(r), ctypes)]


def _job_key(url: str) -> str:
    """The same posting under different addresses (Workday's /en-US/ prefix, query strings) has one key."""
    info = parse_ats_url(url or "")
    if info and info.get("job"):
        return info["key"] + "|" + info["job"].split("?")[0].rstrip("/").lower()
    return (url or "").split("?")[0].split("#")[0].rstrip("/").lower()


# ------------------------------------------------------------------ a company's careers system from its name or URL
def _careers_cache_path():
    return J.CACHE_DIR / "company_careers.json"


def _careers_cache() -> dict:
    try:
        data = json.loads(_careers_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_careers(key: str, value: dict | None) -> None:
    with _LEARN_LOCK:
        data = _careers_cache()
        data[key] = {"time": time.time(), "info": value}
        path = _careers_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _verify(info: dict, name: str = "") -> dict | None:
    rows = _try(lambda i: fetch_board(i), info) or []
    if not rows:
        return None
    out = {k: v for k, v in info.items() if k != "job"}
    out["company"] = name or rows[0].get("company") or ""
    out["open_roles"] = len(rows)
    out["url"] = info.get("url") or _board_url(info)
    return out


def _board_url(info: dict) -> str:
    ats = info.get("ats")
    return {"greenhouse": f"https://job-boards.greenhouse.io/{info.get('slug')}",
            "lever": f"https://jobs.lever.co/{info.get('slug')}",
            "ashby": f"https://jobs.ashbyhq.com/{info.get('slug')}",
            "smartrecruiters": f"https://jobs.smartrecruiters.com/{info.get('slug')}",
            "recruitee": f"https://{info.get('slug')}.recruitee.com",
            "workable": f"https://apply.workable.com/{info.get('slug')}",
            "jobvite": f"https://jobs.jobvite.com/{info.get('slug')}/jobs",
            "workday": f"https://{info.get('tenant')}.{info.get('wd')}.myworkdayjobs.com/{info.get('site')}"}.get(ats, "")


def _known_board(name: str) -> dict | None:
    for company, tenant, wd, site, ctype in WORKDAY:
        if similar(company, name) or similar(tenant, name):
            return {"ats": "workday", "tenant": tenant, "wd": wd, "site": site, "company": company, "ctype": ctype,
                    "key": f"workday:{tenant}|{wd}|{site}"}
    for company, cid, ctype in SMARTRECRUITERS:
        if similar(company, name):
            return {"ats": "smartrecruiters", "slug": cid, "company": company, "ctype": ctype, "key": f"smartrecruiters:{cid}"}
    for slug, ats in J.COMPANY_BOARDS.items():
        if norm_name(slug) == norm_name(name):
            return {"ats": ats, "slug": slug, "company": _title(slug), "key": f"{ats}:{slug}"}
    for entry in learned().values():
        if isinstance(entry, dict) and entry.get("company") and similar(entry["company"], name):
            return dict(entry)
    return None


def _guess_board(name: str, display: str = "", min_exact: int = 5) -> dict | None:
    """Try the usual board addresses (acme, acme-labs...). Greenhouse names its boards, so that one is checked
    against the company's name; Lever/Ashby need an exact slug of at least `min_exact` letters (shorter ones only
    when the company's own website points to them)."""
    display = display or name
    plain = norm_name(name)
    if len(plain) < 3:
        return None
    dashed = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    for slug in dict.fromkeys([plain, dashed]):
        try:
            meta = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}", timeout=10)
            if similar(meta.get("name") or "", display) or similar(meta.get("name") or "", name):
                got = _verify({"ats": "greenhouse", "slug": slug, "key": f"greenhouse:{slug}"}, meta.get("name"))
                if got:
                    return got
        except Exception:
            pass
    if len(plain) >= min_exact:
        for ats in ("ashby", "lever"):
            got = _verify({"ats": ats, "slug": plain, "key": f"{ats}:{plain}"})
            if got:
                got["company"] = display
                return got
    return None


def _ats_links(html: str) -> list[dict]:
    out, seen = [], set()
    for m in re.finditer(r"https?://[^\s\"'<>]+", html or ""):
        info = parse_ats_url(m.group(0))
        if info and info["key"] not in seen:
            seen.add(info["key"])
            out.append(info)
    return out


def _scan_site(url: str, name: str = "") -> dict | None:
    """A company website or careers page: the careers system it links to, else the careers page itself."""
    try:
        html = _get_text(url, timeout=10)
    except Exception:
        return None
    for info in _ats_links(html):
        got = _verify(info, name)
        if got:
            return got
    careers = None
    for m in re.finditer(r'href="([^"]+)"[^>]*>([^<]{0,60})', html):
        href, label = m.group(1), m.group(2)
        if re.search(r"career|jobs|join[- ]us|work[- ]with[- ]us|hiring", href + " " + label, re.I):
            careers = requests.compat.urljoin(url, href)
            break
    pages = html
    if careers and careers.rstrip("/") != url.rstrip("/"):
        try:
            page = _get_text(careers, timeout=10)
            pages += page
            for info in _ats_links(page):
                got = _verify(info, name)
                if got:
                    return got
        except Exception:
            pass
    # Many careers pages load their job board by script: try the board address from the domain ("sima.ai" -> sima,
    # simaai) when it matches the company's name. Short names (3-4 letters) belong to many companies, so those only
    # when the page mentions that careers system.
    host = (requests.compat.urlparse(url).hostname or "").lower().removeprefix("www.")
    label = host.split(".")[0] if host else ""
    mentions = bool(re.search(r"ashby|lever\.co|greenhouse", pages, re.I))
    if label and (not name or similar(label, name) or similar(host.replace(".", ""), name)):
        for cand in dict.fromkeys([norm_name(host.replace(".", "")), label]):
            got = _guess_board(cand, name or label, min_exact=3 if mentions else 5)
            if got:
                return got
    if careers and careers.rstrip("/") != url.rstrip("/"):
        return {"ats": None, "url": careers, "company": name, "key": "site:" + careers}
    return None


def _search_board(name: str, hint: str = "") -> dict | None:
    rows = _try(lambda q: _search(q, "wt-wt", 10, None), f'"{name}" careers jobs {hint}'.strip()) or []
    site = None
    for r in rows:
        url = r.get("href") or r.get("url") or ""
        info = parse_ats_url(url)
        if info and similar(info.get("slug") or info.get("tenant") or "", name):
            got = _verify(info, name)
            if got:
                return got
        host = (requests.compat.urlparse(url).hostname or "").lower().removeprefix("www.")
        if not site and host and similar(host.split(".")[0], name):
            site = f"https://{host}/"
    return _scan_site(site, name) if site else None


def resolve_company(text: str, hint: str = "", website: str = "", use_search: bool = True,
                    guess: bool = True) -> dict | None:
    """Find a company's careers board from its name, careers URL or website, and remember it.
    Returns {ats, company, url, open_roles, key...}; ats is None when only a careers page was found.
    guess=False skips trying board addresses from the name (for plain-word names that belong to many companies)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("http"):
        info = parse_ats_url(raw)
        got = _verify(info) if info else _scan_site(raw)
        if got and got.get("ats"):
            learn(got, company=got.get("company") or "", via="user")
        return got
    key = norm_name(raw)
    cached = _careers_cache().get(key)
    if cached and time.time() - cached.get("time", 0) < (14 if cached.get("info") else 3) * 86400:
        return cached.get("info")
    got = _known_board(raw)
    if got and not got.get("url"):
        got["url"] = _board_url(got)
    if not got and website:
        got = _scan_site(website, raw)
    if not got and guess:
        got = _guess_board(raw)
    if not got and use_search:
        got = _search_board(raw, hint)
    if got and got.get("ats"):
        learn(got, company=got.get("company") or raw, via="user")
    if got or use_search:
        _save_careers(key, got)
    return got


# ------------------------------------------------------------------ job details + enrichment for ranking
_WD_JOB = re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)(/job/[^?#]+)")
_SR_JOB = re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)/(\d{6,})")
_MS_JOB = re.compile(r"https?://apply\.careers\.microsoft\.com/careers/job/(\d+)")
_ATL_JOB = re.compile(r"atlassian\.com/company/careers/details/(\d+)")


def details(url: str) -> dict | None:
    """Full posting for Workday, SmartRecruiters, Microsoft and Atlassian URLs (None for anything else)."""
    m = _WD_JOB.match(url or "")
    if m:
        tenant, wd, site, path = m.groups()
        data = _get_json(f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}")
        info = data.get("jobPostingInfo") or {}
        where = ", ".join(x for x in [info.get("location") or "", *(info.get("additionalLocations") or [])] if x)
        known = next((c for c, t, *_ in WORKDAY if t == tenant), "")
        return {"title": info.get("title"), "company": known or (data.get("hiringOrganization") or {}).get("name") or _title(tenant),
                "location": where, "posted": info.get("startDate") or info.get("postedOn"), "time_type": info.get("timeType"),
                "description": J._clean_html(info.get("jobDescription"))[:6000], "url": url,
                "apply_url": url.rstrip("/") + "/apply", "how_to_apply": WORKDAY_HOW}
    m = _SR_JOB.match(url or "")
    if m:
        data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
        sections = ((data.get("jobAd") or {}).get("sections") or {})
        text = "\n".join(f"{(s or {}).get('title') or k}:\n{J._clean_html((s or {}).get('text'))}"
                         for k, s in sections.items() if (s or {}).get("text"))
        loc = data.get("location") or {}
        return {"title": data.get("name"), "company": (data.get("company") or {}).get("name"),
                "location": loc.get("fullLocation") or loc.get("city"), "posted": data.get("releasedDate"),
                "experience": (data.get("experienceLevel") or {}).get("label"), "description": text[:6000],
                "url": data.get("postingUrl") or url, "apply_url": data.get("applyUrl") or url,
                "how_to_apply": "SmartRecruiters form at the apply link (usually no account needed): fill it, upload the "
                                "tailored PDF, then Submit (the user approves)."}
    m = _MS_JOB.match(url or "")
    if m:
        data = _get_json("https://apply.careers.microsoft.com/api/pcsx/position_details",
                         {"position_id": m.group(1), "domain": "microsoft.com", "hl": "en"}).get("data") or {}
        return {"title": data.get("name"), "company": "Microsoft", "location": "; ".join(data.get("locations") or []),
                "posted": data.get("postedTs"), "description": J._clean_html(data.get("jobDescription"))[:6000],
                "url": data.get("publicUrl") or url, "apply_url": data.get("publicUrl") or url,
                "how_to_apply": "Microsoft careers: Apply asks for a Microsoft account sign-in (list_accounts / request_credentials)."}
    m = _ATL_JOB.search(url or "")
    if m:
        x = next((x for x in _atlassian_listings() or [] if str(x.get("id")) == m.group(1)), None)
        if x:
            text = "\n".join(J._clean_html(x.get(k) or "") for k in ("overview", "responsibilities", "qualifications"))
            return {"title": x.get("title"), "company": "Atlassian", "location": "; ".join(x.get("locations") or []),
                    "description": text[:6000], "url": url,
                    "apply_url": x.get("applyUrl") or (x.get("portalJobPost") or {}).get("portalUrl"),
                    "how_to_apply": "Atlassian's application form (iCIMS) at the apply link; it may ask to create an account."}
    return None


def _detail_text(job: dict) -> str | None:
    ref = job.get("ref") or ""
    if ref.startswith("greenhouse:"):
        _, slug, jid = ref.split(":", 2)
        return J._clean_html(_get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{jid}", timeout=10).get("content"))
    found = details(job.get("url") or "")
    return (found or {}).get("description")


def can_enrich(job: dict) -> bool:
    url = job.get("url") or ""
    return not job.get("text") and bool((job.get("ref") or "").startswith("greenhouse:") or _WD_JOB.match(url)
                                        or _SR_JOB.match(url) or _MS_JOB.match(url))


def enrich(rows: list[dict], budget: float = 8.0, max_jobs: int = 16) -> int:
    """Read the description of the best jobs whose search result had none, so experience requirements count in the
    ranking ('needs 5+ yrs'). Returns how many were read."""
    targets = [r for r in rows if can_enrich(r)][:max_jobs]
    texts = pmap(_detail_text, targets, workers=8, budget=budget)
    n = 0
    for row, text in zip(targets, texts):
        if text:
            row["text"] = J.job_text(text)
            n += 1
    return n


def sites_count() -> dict:
    """How many company career sites the sources cover (for honest reporting)."""
    boards = learned()
    return {"greenhouse_lever_ashby": len(J.COMPANY_BOARDS) + sum(1 for e in boards.values() if e.get("ats") in ("greenhouse", "lever", "ashby")),
            "workday": len(WORKDAY) + sum(1 for e in boards.values() if e.get("ats") == "workday"),
            "smartrecruiters": len(SMARTRECRUITERS) + sum(1 for e in boards.values() if e.get("ats") == "smartrecruiters"),
            "other": sum(1 for e in boards.values() if e.get("ats") in ("recruitee", "jobvite")),
            "big_tech": len(BIG_TECH)}
