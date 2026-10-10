"""Jobs and freelance work.

find_jobs searches company career sites directly, in any country: Workday (Salesforce, Adobe, Nvidia, Cisco, Accenture,
banks...), Greenhouse / Lever / Ashby / SmartRecruiters boards, Amazon / Microsoft / Google / Netflix / Atlassian, YC
startups, Hacker News "Who is hiring", recently funded startups, Indian boards (Instahyre, Cutshort, foundit), The Muse,
Arbeitnow and remote boards; a web search finds more company job pages for the role and place. LinkedIn is only a
fallback. Every job is ranked against the user's resume and saved preferences (match score + reasons, like Jobright).
The extra sources live in job_sources.py and funding.py."""
from __future__ import annotations

import html as htmllib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from ..config import DATA_DIR, settings
from ..memory import applications_store, memory_store, now
from ..registry import CONFIRM, P, tool
from .web import HEADERS

CACHE_DIR = DATA_DIR / "cache"
LINKEDIN_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
TPR = {1: "r86400", 7: "r604800", 30: "r2592000"}
LEVELS = {"internship": "1", "entry": "2", "associate": "3", "mid": "4", "senior": "4"}

# Career boards verified to exist (company slug -> applicant tracking system). Users can add more via preferences.
COMPANY_BOARDS = {s: "greenhouse" for s in """vercel stripe airbnb figma discord robinhood coinbase dropbox reddit gitlab
databricks cloudflare datadog twilio instacart lyft pinterest asana brex gusto duolingo affirm chime samsara anthropic
scaleai mercury webflow airtable mongodb elastic intercom okta block toast flexport nuro checkr faire calendly lattice
attentive warp planetscale fivetran mixpanel launchdarkly algolia contentful storyblok descript tailscale bitwarden""".split()}
COMPANY_BOARDS.update({s: "ashby" for s in """openai linear ramp vanta posthog supabase replit perplexity elevenlabs zapier
notion plaid clickhouse sentry miro snowflake confluent benchling modal runway cohere zed resend clerk render railway
temporal amplitude sanity gamma 1password doppler""".split()})
COMPANY_BOARDS.update({s: "lever" for s in "palantir spotify meesho cred neon fly".split()})
# Indian startups (verified): more product/APM roles in India
COMPANY_BOARDS.update({s: "greenhouse" for s in "groww inmobi slice glance porter druva hackerrank kite tide clear stage verse bluestone".split()})
COMPANY_BOARDS.update({s: "lever" for s in "paytm fi fampay pocketfm mindtickle zeta".split()})
COMPANY_BOARDS.update({s: "ashby" for s in "navi spotdraft sarvam atlan bureau".split()})
# More verified boards (Oct 2026): India, Europe, US product companies and a services firm. Slugs that belong to a
# different company than the name suggests (tcs, bcg, wise, remote on Greenhouse) are deliberately not here.
COMPANY_BOARDS.update({s: "greenhouse" for s in """razorpaysoftwareprivatelimited observeai thoughtworks squarespace twitch
roblox doordashusa monzo n26 hellofresh celonis doctolib adyen deliveroo gocardless xendit cultureamp coursera udemy
khanacademy smartsheet justworks truelayer""".split()})
COMPANY_BOARDS.update({s: "ashby" for s in "backmarket qonto alan mollie paddle multiverse airwallex clickup oyster".split()})
COMPANY_BOARDS.update({s: "lever" for s in "contentsquare nium".split()})
BOARD_NAMES = {"razorpaysoftwareprivatelimited": "Razorpay", "observeai": "Observe.AI", "doordashusa": "DoorDash",
               "hellofresh": "HelloFresh", "gocardless": "GoCardless", "cultureamp": "Culture Amp",
               "khanacademy": "Khan Academy", "backmarket": "Back Market", "clickup": "ClickUp", "truelayer": "TrueLayer",
               "contentsquare": "Contentsquare", "scaleai": "Scale AI", "elevenlabs": "ElevenLabs", "posthog": "PostHog",
               "launchdarkly": "LaunchDarkly", "planetscale": "PlanetScale", "pocketfm": "Pocket FM", "fampay": "FamPay",
               "hackerrank": "HackerRank", "inmobi": "InMobi", "mindtickle": "MindTickle", "spotdraft": "SpotDraft",
               "1password": "1Password", "clickhouse": "ClickHouse", "mongodb": "MongoDB", "gitlab": "GitLab",
               "github": "GitHub", "openai": "OpenAI", "doordash": "DoorDash", "smartsheet": "Smartsheet"}
SERVICE_BOARDS = {"thoughtworks"}

YC_ROLES = {"software-engineer": r"engineer|developer|frontend|backend|fullstack|software|programmer|devops|mobile|data engineer",
            "designer": r"design|ui|ux|product designer|graphic|motion", "product-manager": r"product manager|\bpm\b|product",
            "marketing": r"marketing|growth|seo|content|social media", "sales-manager": r"sales|account executive|business development",
            "support": r"support|customer success", "operations": r"operations|ops\b|finance|analyst",
            "recruiting-hr": r"recruit|talent|\bhr\b|people", "science": r"scien|research|machine learning|\bml\b|\bai\b"}
YC_LOCATIONS = {"remote": "remote", "india": "india", "san francisco": "san-francisco", "new york": "new-york", "nyc": "new-york",
                "los angeles": "los-angeles", "seattle": "seattle", "boston": "boston", "austin": "austin", "chicago": "chicago"}
WWR_CATEGORIES = [(r"frontend|front end|react|vue|angular|ui engineer", "remote-front-end-programming-jobs"),
                  (r"backend|back end|python|django|node|golang|java\b|api", "remote-back-end-programming-jobs"),
                  (r"fullstack|full stack|engineer|developer|software", "remote-full-stack-programming-jobs"),
                  (r"design|ui|ux|figma", "remote-design-jobs"), (r"devops|sre|infrastructure|cloud", "remote-devops-sysadmin-jobs"),
                  (r"product", "remote-product-jobs"), (r"support|customer", "remote-customer-support-jobs"),
                  (r"marketing|sales|growth|seo", "remote-sales-and-marketing-jobs")]

_NORM = [(r"front[\s-]?end", "frontend"), (r"back[\s-]?end", "backend"), (r"full[\s-]?stack", "fullstack"),
         (r"\bdevelopers?\b", "engineer"), (r"\bdevs?\b", "engineer"), (r"\bengineers\b", "engineer"),
         (r"\bswe\b", "software engineer"), (r"\bui\s*/\s*ux\b|\bux\s*/\s*ui\b", "ui ux"), (r"\bjs\b", "javascript"),
         (r"\bml\b", "machine learning"), (r"\bapms?\b", "associate product manager"), (r"\bpms\b", "product manager"),
         (r"\bmanagement\b", "manager"), (r"\bmgr\b", "manager"), (r"\bmanagers\b", "manager")]
_STOP = {"a", "an", "the", "and", "or", "of", "for", "in", "at", "to", "with", "job", "jobs", "role", "roles", "remote",
         "position", "entry", "level", "junior", "senior", "new", "grad", "fresher", "freshers", "internship", "intern",
         "associate", "assoc", "jr", "sr", "lead", "staff", "principal", "head", "trainee", "graduate", "early", "career",
         "i", "ii", "iii", "iv"}
SKILLS = ["react", "next.js", "nextjs", "vue", "angular", "svelte", "javascript", "typescript", "html", "css", "tailwind",
          "sass", "redux", "node", "node.js", "express", "python", "django", "flask", "fastapi", "java", "spring", "golang",
          "rust", "c++", "c#", ".net", "php", "laravel", "ruby", "rails", "kotlin", "swift", "flutter", "react native",
          "android", "ios", "sql", "postgres", "mysql", "mongodb", "graphql", "aws", "gcp", "azure", "docker",
          "kubernetes", "terraform", "linux", "figma", "sketch", "photoshop", "illustrator", "after effects", "premiere",
          "motion graphics", "ui", "ux", "product design", "user research", "wordpress", "shopify", "webflow", "framer",
          "seo", "analytics", "machine learning", "llm", "ai", "data analysis", "excel", "product management", "agile",
          "copywriting", "marketing", "sales", "customer success", "three.js", "webgl", "chrome extension"]
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|head|director|vp|vice president|architect|iii|iv|"
                     r"group product manager|gpm)\b", re.I)
_JUNIOR = re.compile(r"\b(junior|jr\.?|entry|new grad|graduate|intern|internship|associate|assistant|apm|trainee|fresher|"
                     r"early career|manager i|engineer i|analyst i)\b", re.I)
_COUNTRY_ALIASES = {"us": ["united states", "usa", "u.s.", " us", "us ", "us,", "(us)", "americas", "san francisco", "new york", "ny,", "ca,"],
                    "uk": ["united kingdom", "uk", "london", "england", "scotland", "britain"],
                    "india": ["india", "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "delhi", "gurugram", "chennai", "noida"],
                    "europe": ["europe", "eu", "emea", "germany", "berlin", "netherlands", "amsterdam", "france", "paris", "spain", "ireland", "poland"],
                    "canada": ["canada", "toronto", "vancouver", "montreal"], "worldwide": ["worldwide", "anywhere", "global"]}


def norm(text: str) -> str:
    out = (text or "").lower()
    for pattern, repl in _NORM:
        out = re.sub(pattern, repl, out)
    return out


def terms(query: str) -> list[str]:
    places = {w for names in _COUNTRY_ALIASES.values() for n in names for w in n.split()} | set(_COUNTRY_ALIASES)
    places |= {w for k in YC_LOCATIONS for w in k.split()}
    return [w for w in re.findall(r"[a-z0-9+#.]+", norm(query))
            if w not in _STOP and w not in places and len(w) > 1]


def skills_in(text: str) -> set[str]:
    low = " " + norm(text) + " "
    return {s for s in SKILLS if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", low)}


def _clean_html(text: str) -> str:
    return BeautifulSoup(htmllib.unescape(text or ""), "html.parser").get_text(" ", strip=True)


_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*\d{1,2}\s*\+?\s*(?:years|yrs)")
_YEARS_PATTERNS = (r"(\d{1,2})\s*\+\s*(?:years|yrs)",                                   # 3+ years
                   r"(?:minimum|at least|min\.?)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs)",  # at least 2 years
                   r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s+)?(?:\w+\s+){0,3}?experience")  # 2 years of PM experience


def required_years(text: str) -> int | None:
    """Strictest experience requirement stated in a posting, e.g. '3+ yrs overall with 1+ yrs in PM' -> 3,
    '2-4 years' -> 2 (a range counts by its lower bound)."""
    low = (text or "").lower()
    found = [int(n) for n in _RANGE_RE.findall(low)]
    low = _RANGE_RE.sub(" ", low)
    found += [int(n) for pattern in _YEARS_PATTERNS for n in re.findall(pattern, low)]
    found = [n for n in found if 0 < n < 20]
    return max(found) if found else None


def job_text(description: str, limit: int = 700) -> str:
    """Short description plus every sentence that states an experience requirement (they're often at the end)."""
    text = re.sub(r"\s+", " ", description or "").strip()
    reqs = [s.strip() for s in re.split(r"(?<=[.;!?])\s+|\n", text) if re.search(r"\d{1,2}\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*(?:years|yrs)", s, re.I)]
    return (text[:limit] + " | " + " ".join(reqs[:4])[:500]).strip(" |")


def _txt(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _to_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        text = str(value).strip()
        if re.fullmatch(r"\d{9,13}", text):
            return _to_dt(int(text))
        return datetime.fromisoformat(text.replace("Z", "+00:00")[:25] if "T" in text else text[:10] + "T00:00:00+00:00")
    except (ValueError, OSError, OverflowError):
        return None


def _age_days(job: dict) -> float | None:
    dt = _to_dt(job.get("posted"))
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


def _get_json(url: str, params: dict | None = None, timeout: int = 20):
    resp = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ----------------------------------------------------------------- sources
def src_linkedin(query: str, locations: list[str], remote: bool, days: int | None, level: str | None, limit: int) -> list[dict]:
    named = [loc for loc in locations if loc.lower() not in ("remote", "anywhere", "worldwide")][:3]
    places = named or ["Worldwide"]
    out = []
    for place in places:
        params = {"keywords": query, "location": place, "start": 0}
        if not named and remote:  # no place given: worldwide remote jobs only
            params["f_WT"] = "2"
        if days:
            params["f_TPR"] = TPR[min(TPR, key=lambda d: abs(d - days))]
        if level in LEVELS:
            params["f_E"] = LEVELS[level]
        for start in (0, 10):
            params["start"] = start
            resp = requests.get(LINKEDIN_SEARCH, params=params, headers=HEADERS, timeout=20)
            cards = BeautifulSoup(resp.text, "html.parser").select("div.base-card") if resp.status_code == 200 else []
            if not cards:
                break
            for c in cards:
                link = c.select_one("a.base-card__full-link")
                posted = c.select_one("time")
                out.append({"source": "LinkedIn", "title": _txt(c.select_one(".base-search-card__title")),
                            "company": _txt(c.select_one(".base-search-card__subtitle")),
                            "location": _txt(c.select_one(".job-search-card__location")),
                            "posted": posted.get("datetime") if posted else "",
                            "salary": _txt(c.select_one(".job-search-card__salary-info")),
                            "url": (link.get("href") if link else "").split("?")[0]})
            if len(out) >= limit * len(places):
                break
    return out


def src_yc(query: str, locations: list[str], remote: bool) -> list[dict]:
    low = norm(query)
    roles = [slug for slug, pat in YC_ROLES.items() if re.search(pat, low)] or ["software-engineer"]
    places = {YC_LOCATIONS[k] for loc in locations for k in YC_LOCATIONS if k in loc.lower()}
    if remote:
        places.add("remote")
    urls = [f"https://www.ycombinator.com/jobs/role/{r}" + (f"/{p}" if p else "") for r in roles[:2] for p in (places or {""})]
    out = []
    for url in urls[:6]:
        page = requests.get(url, headers=HEADERS, timeout=25)
        match = re.search(r'data-page="([^"]+)"', page.text)
        if not match:
            continue
        for j in json.loads(htmllib.unescape(match.group(1))).get("props", {}).get("jobPostings", []):
            out.append({"source": "YC", "title": j.get("title"), "company": j.get("companyName"),
                        "batch": j.get("companyBatchName"), "about": j.get("companyOneLiner"),
                        "location": j.get("location"), "salary": j.get("salaryRange"), "equity": j.get("equityRange"),
                        "experience": j.get("minExperience"), "visa": j.get("visa"), "kind": j.get("roleSpecificType"),
                        "skills_text": " ".join(j.get("skills") or []), "posted": j.get("createdAt"),
                        "active": j.get("lastActive"), "url": "https://www.ycombinator.com" + (j.get("url") or ""),
                        "apply": "https://www.workatastartup.com/jobs/" + str(j.get("id"))})
    return out


def src_hn(query: str) -> list[dict]:
    threads = _get_json("https://hn.algolia.com/api/v1/search_by_date",
                        {"tags": "story,author_whoishiring", "hitsPerPage": 6}).get("hits", [])
    thread = next((t for t in threads if "who is hiring" in (t.get("title") or "").lower()), None)
    if not thread:
        return []
    item = _get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}", timeout=40)
    words = terms(query)
    out = []
    for c in item.get("children") or []:
        raw = c.get("text") or ""
        if not raw:
            continue
        text = _clean_html(raw.replace("<p>", "\n"))
        if words and not any(w in norm(text) for w in words):
            continue
        header = text.split("\n", 1)[0][:220]
        parts = [p.strip() for p in header.split("|")]
        loc = " | ".join(p for p in parts[1:] if re.search(r"remote|onsite|on-site|hybrid|anywhere|worldwide|\b(us|uk|eu|usa)\b|"
                                                               r"[A-Z][a-z]+,\s?[A-Z]{2}\b|london|berlin|new york|san francisco|"
                                                               r"india|europe|canada|toronto|nyc|\bsf\b", p, re.I))
        links = re.findall(r'href="([^"]+)"', raw)
        link = next((htmllib.unescape(l) for l in links if "ycombinator.com" not in l), "")
        out.append({"source": "HN Who's Hiring", "title": header, "company": parts[0][:80], "location": loc[:120],
                    "posted": c.get("created_at"), "url": f"https://news.ycombinator.com/item?id={c.get('id')}",
                    "apply": link, "text": text[:2500]})
    return out


def _board(slug: str, ats: str, name: str | None = None) -> list[dict]:
    rows = []
    if ats == "greenhouse":
        for j in _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15).get("jobs", []):
            rows.append({"title": j.get("title"), "location": (j.get("location") or {}).get("name", ""),
                         "url": j.get("absolute_url"), "posted": j.get("first_published") or j.get("updated_at"),
                         "ref": f"greenhouse:{slug}:{j.get('id')}"})
    elif ats == "lever":
        for j in _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15):
            cat = j.get("categories") or {}
            sal = j.get("salaryRange") or {}
            rows.append({"title": j.get("text"), "location": cat.get("location", "") + (" (remote)" if j.get("workplaceType") == "remote" else ""),
                         "url": j.get("hostedUrl"), "apply": j.get("applyUrl"), "posted": j.get("createdAt"),
                         "salary": f"{sal.get('min')}-{sal.get('max')} {sal.get('currency', '')}" if sal.get("min") else "",
                         "text": job_text((j.get("descriptionPlain") or "") + " " +
                                          " ".join(_clean_html(x.get("content")) for x in j.get("lists") or []))})
    elif ats == "ashby":
        for j in _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", {"includeCompensation": "true"}, timeout=15).get("jobs", []):
            comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
            rows.append({"title": j.get("title"), "location": (j.get("location") or "") + (" (remote)" if j.get("isRemote") else ""),
                         "url": j.get("jobUrl"), "apply": j.get("applyUrl"), "posted": j.get("publishedAt"), "salary": comp,
                         "text": job_text(j.get("descriptionPlain") or "")})
    company = name or BOARD_NAMES.get(slug) or slug.replace("-", " ").title()
    for r in rows:
        r["company"] = company
        r["source"] = f"{slug} careers ({ats.title()})"
        r["ats"] = ats
        r["direct"] = True
        if slug in SERVICE_BOARDS:
            r["ctype"] = "service"
    return rows


def src_companies(query: str, extra: list[str]) -> list[dict]:
    from . import job_sources as S
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "company_boards.json"
    boards = dict(COMPANY_BOARDS)
    names: dict[str, str] = {}
    recent = sorted((e for e in S.learned().values() if isinstance(e, dict) and e.get("ats") in ("greenhouse", "lever", "ashby")
                     and e.get("slug")), key=lambda e: -float(e.get("seen", 0)))[:150]
    for e in recent:
        boards.setdefault(e["slug"], e["ats"])
        if e.get("company"):
            names[e["slug"]] = e["company"]
    for item in extra or []:
        info = S._try(lambda x: S.resolve_company(x, use_search=False), item)
        if info and info.get("ats") in ("greenhouse", "lever", "ashby") and info.get("slug"):
            boards.setdefault(info["slug"], info["ats"])
            names[info["slug"]] = info.get("company") or names.get(info["slug"], "")
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    recent = cache.get("time", 0) > time.time() - 6 * 3600
    cached_boards = cache.get("boards", {}) if recent else {}
    missing = {slug: ats for slug, ats in boards.items() if cached_boards.get(slug) != ats}

    def fetch(todo: dict) -> list[dict]:
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=24) as pool:
            for result in pool.map(lambda kv: _safe(_board, kv[0], kv[1], names.get(kv[0]) or None), todo.items()):
                rows.extend(result)
        return rows

    if recent and not missing:
        jobs = cache["jobs"]
    elif recent and len(missing) <= 40:          # a few newly learned boards: read just those
        jobs = cache["jobs"] + fetch(missing)
        cache_file.write_text(json.dumps({"time": cache["time"], "boards": {**cached_boards, **missing}, "jobs": jobs},
                                         ensure_ascii=False), encoding="utf-8")
    else:
        jobs = fetch(boards)
        cache_file.write_text(json.dumps({"time": time.time(), "boards": boards, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
    words = terms(query)
    return [j for j in jobs if not words or any(w in norm(j.get("title", "")) for w in words)]


def src_remote(query: str) -> list[dict]:
    out = []
    try:
        for j in _get_json("https://remotive.com/api/remote-jobs", {"search": query, "limit": 30}).get("jobs", []):
            out.append({"source": "Remotive", "title": j.get("title"), "company": j.get("company_name"),
                        "location": "Remote: " + (j.get("candidate_required_location") or ""), "posted": j.get("publication_date"),
                        "salary": j.get("salary"), "url": j.get("url"), "text": _clean_html(j.get("description", ""))[:1500]})
    except Exception:
        pass
    try:
        tag = (terms(query) or [""])[0]
        for j in _get_json("https://jobicy.com/api/v2/remote-jobs", {"count": 30, "tag": tag}).get("jobs", []):
            out.append({"source": "Jobicy", "title": j.get("jobTitle"), "company": j.get("companyName"),
                        "location": "Remote: " + str(j.get("jobGeo") or ""), "posted": j.get("pubDate"), "url": j.get("url"),
                        "text": _clean_html(j.get("jobExcerpt", ""))[:800]})
    except Exception:
        pass
    try:
        for j in _get_json("https://himalayas.app/jobs/api/search", {"q": query, "limit": 30}).get("jobs", []):
            locs = j.get("locationRestrictions") or []
            out.append({"source": "Himalayas", "title": j.get("title"), "company": j.get("companyName"),
                        "location": "Remote: " + (", ".join(locs) if isinstance(locs, list) and locs else "Worldwide"),
                        "posted": j.get("pubDate"), "url": j.get("applicationLink") or j.get("guid"),
                        "text": (j.get("excerpt") or "")[:800]})
    except Exception:
        pass
    try:
        low = norm(query)
        category = next((c for pat, c in WWR_CATEGORIES if re.search(pat, low)), "remote-full-stack-programming-jobs")
        rss = requests.get(f"https://weworkremotely.com/categories/{category}.rss", headers=HEADERS, timeout=20)
        for item in BeautifulSoup(rss.text, "xml").select("item")[:40]:
            title = _txt(item.find("title"))
            company, _, role = title.partition(":")
            region = _txt(item.find("region")) or "Anywhere"
            out.append({"source": "WeWorkRemotely", "title": role.strip() or title, "company": company.strip(),
                        "location": "Remote: " + region, "posted": _txt(item.find("pubDate")), "url": _txt(item.find("link")),
                        "text": _clean_html(_txt(item.find("description")))[:1200]})
    except Exception:
        pass
    return out


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return []


# ----------------------------------------------------------------- matching
def job_preferences() -> dict:
    prof = memory_store.load().get("profile", {})
    prefs = prof.get("job_preferences") or {}
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except json.JSONDecodeError:
            prefs = {"notes": prefs}
    return prefs if isinstance(prefs, dict) else {}


def user_skills() -> set[str]:
    prof = memory_store.load().get("profile", {})
    text = " ".join(str(v) for v in prof.values())
    prefs = job_preferences()
    text += " " + " ".join(prefs.get("skills") or [])
    try:
        if settings.resume_path.exists():
            text += " " + read_resume()
    except Exception:
        pass
    return skills_in(text)


def _location_fit(job: dict, locations: list[str], remote_ok: bool) -> tuple[int, str, bool]:
    """Returns (points, reason, acceptable). Works for any country: its names, other names and main cities count,
    matched as whole words."""
    from .job_sources import city_names, has_place, is_city, place_aliases, place_key
    loc = " " + norm(job.get("location") or "") + " "
    is_remote = bool(re.search(r"\b(remote|anywhere|worldwide|work from home|wfh)\b", loc))
    wanted = [l.lower().strip() for l in locations if l.strip()]
    places = [w for w in wanted if w not in ("remote", "anywhere", "worldwide")]
    if not wanted:
        return 10, ("remote" if is_remote else ""), True
    for place in places:
        if is_city(place):
            if any(has_place(loc, a) for a in city_names(place)):
                return 15, f"in {place.title()}", True
            continue
        aliases = set(_COUNTRY_ALIASES.get(place, [])) | {place}
        for country, names in _COUNTRY_ALIASES.items():
            if place in [n.strip() for n in names]:
                aliases |= set(names) | {country}
        aliases |= set(place_aliases(place))
        if any(has_place(loc, a) for a in aliases):
            return 15, f"in {place.title()}", True
    for place in places:            # a city the user named: elsewhere in the same country still fits, ranked lower
        country = place_key(place) if is_city(place) else None
        if country and any(has_place(loc, a) for a in place_aliases(country)):
            if is_remote:
                return 13, f"remote ({country.title()})", True
            return 9, f"in {country.title()}, not {place.title()}", True
    if is_remote and (remote_ok or "remote" in wanted):
        from .job_sources import COUNTRIES, place_key, region_countries
        short = [c for c, names in _COUNTRY_ALIASES.items() if c != "worldwide" and any(has_place(loc, n) for n in names)]
        full = [k for k, c in COUNTRIES.items() if any(has_place(loc, n) for n in (k, *c["aka"]) if len(n) > 3)]
        restricted = short + full
        region = re.search(r"remote\s*[:(]\s*([^)]+)", loc)
        region_text = region.group(1).strip(" ,.") if region else ""
        named_region = bool(region_text) and not re.search(r"worldwide|anywhere|global|everywhere|^remote$", region_text)
        allowed = {c for p in places for c, names in _COUNTRY_ALIASES.items() if p == c or p in [n.strip() for n in names]}
        allowed |= {k for p in places for k in ([place_key(p)] if place_key(p) else region_countries(p))}
        pieces = [re.sub(r"\b(only|based|region|regions|timezones?|time zones?|countries)\b", "", p).strip()
                  for p in re.split(r"[,/;|&]| and | or ", region_text)]
        region_ok = any(set(region_countries(p)) & allowed for p in pieces if p)
        if places and (restricted or named_region) and not (set(restricted) & allowed) and not region_ok:
            label = (", ".join(k.title() for k in dict.fromkeys(full)) or ", ".join(s.upper() for s in dict.fromkeys(short))
                     or region_text.title())
            return -15, f"remote but limited to {label}", True
        return 13, "remote", True
    return 0, "", False


def score_job(job: dict, query_terms: list, skills: set[str], prefs: dict) -> tuple[int, list[str]]:
    title = norm(job.get("title") or "")
    body = " ".join(str(job.get(k) or "") for k in ("title", "text", "skills_text", "kind", "about"))
    reasons: list[str] = []
    score = 20
    role_sets = [t for t in (query_terms if query_terms and isinstance(query_terms[0], list) else [query_terms]) if t]
    if role_sets:
        best = max(len([t for t in role if t in title]) / len(role) for role in role_sets)
        score += int(35 * best)
        if best > 0:
            reasons.append("title fits")
    matched = sorted(skills & skills_in(body))
    if matched:
        score += min(20, 5 * len(matched))
        reasons.append("skills: " + ", ".join(matched[:5]))
    level = (prefs.get("level") or "").lower()
    exp = (job.get("experience") or "").lower()
    min_years = required_years(job.get("text") or "")
    if level in ("internship", "entry", "junior", "associate", ""):
        if _SENIOR.search(job.get("title") or ""):
            score -= 30
            reasons.append("senior role")
        elif min_years is not None and min_years >= 3:
            score -= 25
            reasons.append(f"needs {min_years}+ yrs")
        elif (_JUNIOR.search(job.get("title") or "") or "new grad" in exp or "any" in exp
              or re.search(r"\b(entry|intern|internship|fresher|graduate)\b", exp)):
            score += 10
            reasons.append("entry-level friendly")
        if re.search(r"\b([3-9]|1\d)\+? years", exp):
            score -= 10
    elif level in ("mid", "senior") and _SENIOR.search(job.get("title") or ""):
        score += 8
    pts, why, _ = _location_fit(job, prefs.get("locations") or [], bool(prefs.get("remote", True)))
    score += pts
    if why:
        reasons.append(why)
    age = _age_days(job)
    if age is not None:
        if age <= 7:
            score += 10
            reasons.append(f"posted {int(age)}d ago" if age >= 1 else "posted today")
        elif age <= 30:
            score += 4
        elif age > 60:
            score -= 12
            reasons.append("old posting")
    if prefs.get("needs_visa_sponsorship") and "citizen" in (job.get("visa") or "").lower():
        score -= 20
        reasons.append("no visa sponsorship")
    if job.get("salary"):
        score += 2
    src = job.get("source") or ""
    if src in ("YC", "HN Who's Hiring") or "careers (" in src:
        score += 3  # direct from the company / curated
    elif job.get("direct"):
        score += 3  # the company's own careers site (Amazon, Microsoft, Google...)
    if src == "LinkedIn":
        score -= 6  # the same job on the company's own site is better, and LinkedIn is the user's last choice
    elif job.get("via_linkedin"):
        score -= 4
    from .job_sources import company_type
    if company_type(job) == "staffing":
        score -= 5
        reasons.append("via a recruitment agency")
    fund = job.get("funding")
    if isinstance(fund, dict) and (fund.get("round") or fund.get("amount")):
        score += 6
        when = f" ({fund['date']})" if fund.get("date") else ""
        reasons.append("recently raised " + " ".join(x for x in (fund.get("amount"), fund.get("round")) if x) + when)
    return max(1, min(99, score)), reasons


_SEARCH_ASK = re.compile(r"\b(find|search|look for|look up|show me|new|more|other|another|fresh|latest)\b|"
                         r"\bany (job|role|position|opening)s?\b|\b(jobs?|roles?|positions?|openings?) (in|at|near|from)\b|"
                         r"\bapply (to|for) (some|a few|jobs?|roles?)\b", re.I)


def picked_only(args) -> str | None:
    """While the user's pick list is active, Karya works only on the jobs they picked. 2026-10-09: the AI announced
    "the last picked job - OKX", looked up J55 (a job nobody picked), tailored a resume for it and opened a made-up
    link, then skipped a job the user did pick."""
    from .. import answers, apply_queue
    job_id = str(args.get("job_id") or "").strip().upper()
    queue = apply_queue.load()
    left = apply_queue.pending()
    if not job_id or not left or any(str(j.get("id", "")).upper() == job_id for j in queue):
        return None
    if re.search(rf"\b{re.escape(job_id)}\b", " ".join(answers.RECENT_USER[-2:]).upper()):
        return None                       # the user named it
    try:                                  # a search newer than the pick list: its jobs can be looked at before picking
        if (last_jobs_file()).stat().st_mtime > time.time() - apply_queue.age_seconds():
            return None
    except OSError:
        pass
    nxt = left[0]
    return (f"NOT RUN: {job_id} isn't one of the jobs the user picked. Work only on their picks; next: "
            f'{nxt["id"]} "{nxt["title"]}" at {nxt["company"]} ({nxt["url"]}). application_queue shows the list.')


def _search_precheck() -> str | None:
    """While Karya works through the user's picked jobs, a new search only wastes time, unless they asked for one."""
    from .. import answers, apply_queue
    left = apply_queue.pending()
    asked = answers.RECENT_USER[-1] if answers.RECENT_USER else ""
    if not left or _SEARCH_ASK.search(asked):
        return None
    nxt = left[0]
    return (f"NOT RUN: you're working through the user's picked jobs ({len(left)} left), so no new search is needed. "
            f'Next: {nxt["id"]} "{nxt["title"]}" at {nxt["company"]} ({nxt["url"]}). application_queue shows the list.')


DEFAULT_SOURCES = ["companies", "workday", "smartrecruiters", "bigtech", "yc", "hn", "funded", "india", "boards",
                   "remote", "discover"]
NO_LOGIN_DEFAULT = ["companies", "smartrecruiters", "hn", "remote", "discover"]
ALL_SOURCES = DEFAULT_SOURCES + ["linkedin"]
LINKEDIN_BELOW = 15     # LinkedIn is only searched when the other sources find fewer matches than this
SOURCES_BUDGET = 50     # seconds; a source still running then is skipped this time (its caches keep filling)
COMPANY_TYPES = ["product", "service", "startup", "enterprise"]
NO_LOGIN_BOARDS = ("Remotive", "Jobicy", "WeWorkRemotely")


@tool("find_jobs", "Find the best jobs for the user in ANY country, straight from company career sites: Workday "
      "(Salesforce, Adobe, Nvidia, Cisco, Accenture, PwC, Walmart, banks...), Greenhouse/Lever/Ashby/SmartRecruiters "
      "boards of ~170 companies, Amazon/Microsoft/Google/Netflix/Atlassian, YC startups, HN 'Who is hiring', recently "
      "funded startups, Indian boards (Instahyre, Cutshort, foundit), The Muse, Arbeitnow, remote boards, and a web "
      "search that finds more company job pages for the role and place. LinkedIn is only a fallback (or when asked). "
      "Ranks every job against the user's resume and saved preferences with a match score and reasons. Leave "
      "arguments empty to use the saved preferences.", {
    "query": P("string", "Role or keywords; several roles comma-separated, e.g. 'product manager, product analyst'"),
    "locations": P("array", "Countries, cities or regions and/or 'Remote', e.g. ['India'], ['Hyderabad'], "
                            "['United States','UK','Remote'], ['Europe']", items={"type": "string"}),
    "remote": P("boolean", "Include remote jobs (default true)"),
    "level": P("string", "Experience level", enum=["internship", "entry", "associate", "mid", "senior"]),
    "posted_within_days": P("integer", "Only jobs posted in the last N days (default 30)"),
    "company_types": P("array", "Only these kinds of companies: product, service, startup, enterprise (default all)",
                       items={"type": "string", "enum": COMPANY_TYPES}),
    "sources": P("array", "Subset of: " + ", ".join(ALL_SOURCES) + " (default: all but linkedin, which is added when "
                          "few jobs are found)", items={"type": "string"}),
    "no_login": P("boolean", "Only jobs you can apply to on the company's own form without any login"),
    "limit": P("integer", "How many top matches to return (default 25)"),
    "include_applied_companies": P("boolean", "Also show companies the user applied to in the last 60 days (left out "
                                              "by default; jobs they already applied to never show)"),
}, group="jobs", precheck=lambda args: _search_precheck())
def find_jobs(query: str = "", locations: list[str] | None = None, remote: bool | None = None, level: str | None = None,
              posted_within_days: int = 30, sources: list[str] | None = None, limit: int = 25, no_login: bool = False,
              company_types: list[str] | None = None, include_applied_companies: bool = False):
    from .. import answers
    from . import funding
    from . import job_sources as S
    prefs = job_preferences()
    query = query or ", ".join(prefs.get("roles") or []) or "software engineer"
    locs = [l for l in (locations or prefs.get("locations") or []) if l]
    remote = prefs.get("remote", True) if remote is None else remote
    level = level or prefs.get("level") or "entry"
    merged = dict(prefs, locations=locs, remote=remote, level=level)
    ctypes = [str(c).lower().strip() for c in (company_types or prefs.get("company_types") or []) if str(c).strip()]
    explicit = [s.lower().strip() for s in sources] if sources else None
    chosen = explicit or (NO_LOGIN_DEFAULT if no_login else DEFAULT_SOURCES)
    tasks = _source_tasks(query, locs, remote, level, posted_within_days, prefs, ctypes)
    found, counts, errors = _gather(tasks, [s for s in chosen if s != "linkedin"])
    memory = applied_memory()
    asked = query + " " + (answers.RECENT_USER[-1] if answers.RECENT_USER else "")
    found, hidden = skip_applied(found, memory, include_applied_companies, asked)
    q_terms = [terms(role) for role in query.split(",") if terms(role)]
    skills = user_skills()
    funding.tag_rows(found)
    ranked = rank_found(found, q_terms, locs, bool(remote), posted_within_days, merged, ctypes, skills)
    want_linkedin = "linkedin" in chosen or bool(prefs.get("include_linkedin"))
    linkedin_note = ""
    if want_linkedin or (explicit is None and not no_login and len(ranked) < LINKEDIN_BELOW):
        more, more_counts, more_errors = _gather(tasks, ["linkedin"], budget=40)
        more, more_hidden = skip_applied(more, memory, include_applied_companies, asked)
        hidden["jobs"] += more_hidden["jobs"]
        for name, n in more_hidden["companies"].items():
            hidden["companies"][name] = hidden["companies"].get(name, 0) + n
        found += more
        counts.update(more_counts)
        errors += more_errors
        if not want_linkedin:
            linkedin_note = f"Company sites and boards gave {len(ranked)} matches, so LinkedIn was added as a fallback."
        ranked = rank_found(found, q_terms, locs, bool(remote), posted_within_days, merged, ctypes, skills)
    best = [job for _, job, _ in ranked[: max(1, min(limit, 40)) + 10]]
    if S.enrich(best, budget=8):   # descriptions of the best ones that had none: "needs 5+ yrs" now counts
        rescored = [(*score_job(job, q_terms, skills, merged), job) for _, job, _ in ranked]
        ranked = sorted(((m, job, why) for m, why, job in rescored), key=lambda x: -x[0])
    top = []
    for match, job, why in ranked[:max(1, min(limit, 40))]:
        row = job_row(match, job, why)
        if no_login and not row["apply_via"].startswith("company form") and job["source"] not in NO_LOGIN_BOARDS:
            continue
        top.append(row)
    sites = S.sites_count()
    out = {"query": query, "locations": locs or ["anywhere"], "level": level, "scanned": len(found), "matched": len(ranked),
           "per_source": counts, "jobs": top,
           "searched": (f"{sum(sites.values())} company career sites (Workday {sites['workday']}, Greenhouse/Lever/Ashby "
                        f"{sites['greenhouse_lever_ashby']}, SmartRecruiters {sites['smartrecruiters']}, big tech "
                        f"{sites['big_tech']}" + (f", other {sites['other']}" if sites.get("other") else "")
                        + ") + startup lists, job boards and a web search")}
    if ctypes:
        out["company_types"] = ctypes
    if linkedin_note:
        out["linkedin"] = linkedin_note
    left_out = hidden_note(hidden, memory["days"])
    if left_out:
        out["already_applied"] = left_out
    if len(top) < 10:  # the user wants more than a handful: tell the AI how to widen instead of coming back
        tips = []
        if no_login:
            tips.append("drop no_login (Workday, big tech and YC have many more)")
        if posted_within_days and posted_within_days < 45:
            tips.append("raise posted_within_days (e.g. 60)")
        if ctypes:
            tips.append("drop company_types")
        unused = [s for s in ALL_SOURCES if s not in chosen and s not in counts]
        if unused:
            tips.append("add sources: " + ", ".join(unused))
        tips.append("add nearby roles to query (e.g. 'associate product manager, product analyst, program manager')")
        tips.append("add a nearby country or 'Remote' to locations")
        tips.append("find_funded_companies for startups that just raised money")
        tips.append("for freelance clients use search_freelance")
        out["more"] = ("Only " + str(len(top)) + " matched. To find more without coming back to the user: "
                       + "; ".join(tips) + ".")
    if errors:
        out["errors"] = errors
    if not settings.resume_path.exists():
        out["tip"] = "No resume found (RESUME_PATH). Matching uses only the saved profile/preferences."
    while len(json.dumps(out, ensure_ascii=False)) > 9_500 and len(out["jobs"]) > 5:
        out["jobs"].pop()  # results are ranked: drop the weakest so the answer is never cut mid-way
    out["shown"] = len(out["jobs"])
    remember_jobs(out["jobs"], ranked)
    out["next"] = ("Show the list to the user with choose_jobs (they pick jobs and can skip companies), then for each "
                   "picked job: get_job_details(job_id), tailor_resume(job_id), open its apply link and apply.")
    return out


def _source_tasks(query: str, locs: list[str], remote: bool, level: str, days: int, prefs: dict, ctypes: list[str]) -> dict:
    """Every source by name. The functions are looked up when called (tests replace them)."""
    from . import funding
    from . import job_sources as S
    return {"yc": lambda: src_yc(query, locs, remote), "hn": lambda: src_hn(query),
            "companies": lambda: src_companies(query, prefs.get("companies") or [])
            + S._try(lambda q: S.src_learned_other(q, locs, ctypes), query) or [],
            "workday": lambda: S.src_workday(query, locs, ctypes),
            "smartrecruiters": lambda: S.src_smartrecruiters(query, locs, ctypes),
            "bigtech": lambda: S.src_bigtech(query, locs, level, ctypes),
            "india": lambda: S.src_india(query, locs, level, ctypes),
            "boards": lambda: S.src_boards(query, locs, level, ctypes),
            "discover": lambda: S.src_discover(query, locs, level, ctypes),
            "funded": lambda: funding.src_funded(query, locs, level, ctypes),
            "remote": lambda: src_remote(query) if remote or not locs else [],
            "linkedin": lambda: src_linkedin(query, locs, remote, days, level, 15)}


def _gather(tasks: dict, names: list[str], budget: float = SOURCES_BUDGET) -> tuple[list, dict, list]:
    """Run the named sources in parallel. One that fails or is still running after `budget` seconds is reported in
    errors and skipped (it never holds up the answer)."""
    names = [n for n in dict.fromkeys(names) if n in tasks]
    found, counts, errors = [], {}, []
    if not names:
        return found, counts, errors
    pool = ThreadPoolExecutor(max_workers=len(names))
    futures = {n: pool.submit(tasks[n]) for n in names}
    done, _ = wait(list(futures.values()), timeout=budget)
    pool.shutdown(wait=False, cancel_futures=True)
    for name, fut in futures.items():
        if fut not in done:
            errors.append(f"{name}: still running after {int(budget)}s, skipped this time")
            continue
        try:
            rows = fut.result() or []
            counts[name] = len(rows)
            found.extend(rows)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    return found, counts, errors


def _source_order(job: dict) -> int:
    """Company career sites first, LinkedIn last: when the same job is listed twice, the better copy is kept."""
    src = job.get("source") or ""
    if job.get("direct") or "careers (" in src:
        return 0
    if src in ("YC", "HN Who's Hiring"):
        return 1
    if src == "LinkedIn" or job.get("via_linkedin"):
        return 3
    return 2


def rank_found(found: list[dict], q_terms: list, locs: list[str], remote: bool, posted_within_days: int, merged: dict,
               ctypes: list[str] | None = None, skills: set[str] | None = None) -> list[tuple]:
    """Filter (role, place, age, excluded, company type), remove duplicates and score: [(match, job, why)], best first."""
    from .job_sources import company_type, type_ok
    skills = user_skills() if skills is None else skills
    exclude = [norm(x) for x in merged.get("exclude") or []]
    seen, seen_urls, ranked = set(), set(), []
    for job in sorted(found, key=_source_order):
        if not job.get("url") or not job.get("title"):
            continue
        key = (norm(job["title"])[:60], norm(job.get("company") or "")[:40])
        if key in seen or job["url"] in seen_urls or any(x and x in norm(job["title"] + " " + (job.get("company") or ""))
                                                          for x in exclude):
            continue
        if ctypes and not type_ok(company_type(job), ctypes):
            continue
        age = _age_days(job)
        if age is not None and posted_within_days and age > max(posted_within_days, 1) * 1.5:
            continue
        _, _, acceptable = _location_fit(job, locs, bool(remote))
        if not acceptable:
            continue
        if q_terms:
            title_words = set(re.findall(r"[a-z0-9+#.]+", norm(job["title"])))
            fits = [sum(1 for t in role if t in title_words) / len(role) for role in q_terms]
            needed = [1.0 if len(role) <= 2 else 0.66 for role in q_terms]
            if not any(f >= n for f, n in zip(fits, needed)):
                continue  # not the role the user asked for (e.g. "Product Engineer" for "product manager")
        seen.add(key)
        seen_urls.add(job["url"])
        match, why = score_job(job, q_terms, skills, merged)
        ranked.append((match, job, why))
    ranked.sort(key=lambda x: -x[0])
    return ranked


ACCOUNT_SITES = {"workday": "company site on Workday (needs a free account there; Karya can create it)",
                 "amazon": "amazon.jobs (needs an Amazon jobs account)",
                 "microsoft": "Microsoft careers (needs a Microsoft account)",
                 "google": "Google careers (needs a Google account)",
                 "atlassian": "company site (its form may ask you to create an account)",
                 "eightfold": "company site (its form may ask you to create an account)",
                 "instahyre": "Instahyre (free account)", "cutshort": "Cutshort (free account)",
                 "foundit": "foundit (free account)", "muse": "The Muse, then the company's site"}


def apply_via(job: dict) -> str:
    src = job.get("source") or ""
    ats = job.get("ats") or ""
    if src == "LinkedIn" or job.get("via_linkedin"):
        return "LinkedIn (needs LinkedIn login)"
    if src == "YC":
        return "Work at a Startup (free YC account)"
    if src.startswith("HN"):
        return "see the post (email or link)"
    if ats in ACCOUNT_SITES:
        return ACCOUNT_SITES[ats]
    if ats == "smartrecruiters":
        return "company form, no login (SmartRecruiters)"
    if ats in ("recruitee", "workable", "jobvite"):
        return f"company form, usually no login ({ats.title()})"
    if ats in ("greenhouse", "lever", "ashby") or "careers (" in src:
        return "company form, no login"
    return "job board link"


def job_row(match: int, job: dict, why: list[str]) -> dict:
    """One job as shown to the AI/user."""
    from .job_sources import company_type
    posted_dt = _to_dt(job.get("posted"))
    row = {"match": match, "title": job["title"][:140], "company": job.get("company"), "location": job.get("location"),
           "posted": posted_dt.strftime("%Y-%m-%d") if posted_dt else str(job.get("posted") or "")[:10],
           "salary": job.get("salary"), "source": job["source"],
           "url": job["url"], "apply": job.get("apply"), "why": "; ".join(why)}
    if job.get("experience"):
        row["experience"] = job["experience"]
    if job.get("batch"):
        row["yc"] = job["batch"]
    ctype = company_type(job)
    if ctype:
        row["type"] = ctype
    fund = job.get("funding")
    if isinstance(fund, dict) and (fund.get("round") or fund.get("amount")):
        row["funding"] = " ".join(x for x in (fund.get("amount"), fund.get("round"), fund.get("date")) if x)
    row["apply_via"] = apply_via(job)
    return {k: v for k, v in row.items() if v not in (None, "", [])}


def last_jobs_file():
    """The chat's job shortlist (J1, J2...), or a bot's own."""
    from ..runctx import current
    run = current()
    return CACHE_DIR / (f"last_jobs.{run.agent_id}.json" if run.is_bot else "last_jobs.json")


def remember_jobs(rows: list[dict], ranked: list) -> None:
    """Give each shown job a short id (J1, J2...) and keep its full details, so later steps use the id, not a URL.
    Several searches in a row (e.g. LinkedIn, then company sites) build ONE shortlist; ids keep counting."""
    path = last_jobs_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - path.stat().st_mtime > 30 * 60:
            saved = {}
    except (OSError, ValueError):
        saved = {}
    by_saved_url = {v.get("url"): k for k, v in saved.items()}
    from .. import apply_queue
    queued = apply_queue.load()
    by_saved_url.update({j.get("url"): j.get("id") for j in queued if j.get("url") and j.get("id")})
    taken = list(saved) + [j.get("id", "") for j in queued]   # the pick list's ids stay theirs
    next_n = max([int(k[1:]) for k in taken if k[1:].isdigit()] + [0]) + 1
    by_url = {job.get("url"): job for _, job, _ in ranked}
    for row in rows:
        known = by_saved_url.get(row["url"])
        if known:
            row["id"] = known
        else:
            row["id"] = f"J{next_n}"
            next_n += 1
        job = by_url.get(row["url"], {})
        saved[row["id"]] = {**row, "text": (job.get("text") or "")[:1500]}
    rows[:] = [{"id": r["id"], **{k: v for k, v in r.items() if k != "id"}} for r in rows]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")


def cached_jobs() -> dict:
    """The shortlist from the latest find_jobs (id -> job)."""
    try:
        data = json.loads((last_jobs_file()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def job_by_id(job_id: str) -> dict | None:
    key = str(job_id).strip().upper()
    try:
        data = json.loads((last_jobs_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if key in data:
        return data[key]
    from .. import apply_queue
    return next((dict(j) for j in apply_queue.load() if j.get("id", "").upper() == key), None)


def resolve_job_url(job_id: str = "", url: str = "") -> str:
    if job_id:
        job = job_by_id(job_id)
        if job:
            return job.get("url") or url
    return url


@tool("choose_jobs", "Show the user the jobs from the latest find_jobs as a list to review BEFORE applying. They tick the "
      "ones to apply to and can skip whole companies (skipped companies are never shown again). Returns their picks.", {
    "job_ids": P("array", "Job ids to show (default: all from the latest find_jobs)", items={"type": "string"}),
    "note": P("string", "One short line for the user, e.g. why these"),
}, group="jobs")
def choose_jobs(job_ids: list[str] | None = None, note: str = ""):
    return "ERROR: choose_jobs only works from the chat (the user picks in a list)."


@tool("set_job_preferences", "Save the user's job search preferences (used by find_jobs and for applications).", {
    "roles": P("array", "Target roles, e.g. ['frontend engineer','UI developer']", items={"type": "string"}),
    "locations": P("array", "Countries/cities and/or 'Remote', e.g. ['United States','UK','India','Remote']", items={"type": "string"}),
    "remote": P("boolean", "Open to remote jobs"),
    "level": P("string", "Experience level", enum=["internship", "entry", "associate", "mid", "senior"]),
    "skills": P("array", "Main skills to match", items={"type": "string"}),
    "min_salary": P("string", "Minimum salary with currency, e.g. '6 LPA' or '$90k'"),
    "needs_visa_sponsorship": P("boolean", "Needs visa sponsorship for the job's country"),
    "exclude": P("array", "Words/companies to skip", items={"type": "string"}),
    "companies": P("array", "Companies to always check: names or careers-page URLs (e.g. 'Razorpay', "
                            "'https://careers.acme.com'). Karya finds their careers system (Workday, Greenhouse, Lever, "
                            "Ashby, SmartRecruiters...) once and searches it every time", items={"type": "string"}),
    "company_types": P("array", "Kinds of companies to show: product, service, startup, enterprise (empty = all)",
                       items={"type": "string", "enum": COMPANY_TYPES}),
    "include_linkedin": P("boolean", "Search LinkedIn every time (default: only when other sources find few jobs)"),
    "industries": P("array", "Fields the user likes, e.g. ['fintech','AI','SaaS'] (used to rank funded companies)",
                    items={"type": "string"}),
}, group="jobs")
def set_job_preferences(**fields):
    data = memory_store.load()
    profile = data.setdefault("profile", {})
    prefs = profile.get("job_preferences") if isinstance(profile.get("job_preferences"), dict) else {}
    prefs.update({k: v for k, v in fields.items() if v not in (None, "", [])})
    profile["job_preferences"] = prefs
    memory_store.save(data)
    out = {"saved": prefs}
    new = [str(c).strip() for c in fields.get("companies") or [] if str(c).strip()][:10]
    if new:
        from . import job_sources as S
        found = S.pmap(S.resolve_company, new, workers=5, budget=45)
        report = {}
        for name, info in zip(new, found):
            if info and info.get("ats"):
                report[name] = (f"{info['ats']} board" + (f", {info['open_roles']} open jobs" if info.get("open_roles") else "")
                                + f": {info.get('url')}")
            elif info:
                report[name] = f"careers page {info.get('url')} (no job feed; open it in the browser)"
            else:
                report[name] = "careers system not found yet - give its careers page URL"
        out["companies"] = report
    return out


APP_FIELDS = ["full_name", "email", "phone", "location", "linkedin", "github", "portfolio", "current_title",
              "years_experience", "work_authorization", "needs_visa_sponsorship", "notice_period", "expected_salary",
              "education", "pronouns"]


@tool("get_application_profile", "Everything needed to fill job applications (name, contact, links, work authorization, "
      "resume path...). Lists 'missing' fields; ask the user once for them and save with update_profile.", group="jobs")
def get_application_profile():
    prof = memory_store.load().get("profile", {})
    prefs = job_preferences()
    name = prof.get("name") or prof.get("full_name") or ""
    first, _, last = name.partition(" ")
    out = {"first_name": first, "last_name": last, "full_name": name}
    for field in APP_FIELDS[1:]:
        value = prof.get(field)
        if value in (None, "") and field in prefs:
            value = prefs[field]
        if field == "portfolio" and not value:
            value = prof.get("website") or prof.get("projects", "")[:200]
        if field == "current_title" and not value:
            value = prof.get("headline")
        out[field] = value
    out["skills"] = prof.get("skills") or ", ".join(prefs.get("skills") or [])
    out["resume_path"] = str(settings.resume_path) if settings.resume_path.exists() else None
    for field in ("current_salary", "gender", "date_of_birth"):
        if prof.get(field):
            out[field] = prof[field]
    from .. import answers as answer_store
    saved = answer_store.saved_answers()          # passwords and codes are never in here
    if saved:
        out["saved_form_answers"] = dict(list(saved.items())[-25:])
    missing = [f for f in ("email", "phone", "location", "linkedin", "work_authorization", "years_experience",
                           "notice_period", "current_salary", "expected_salary") if not out.get(f)]
    if not out["resume_path"]:
        missing.append("resume (set RESUME_PATH)")
    out["missing"] = missing
    out["rule"] = ("Questions about notice period, salary, gender, years of a specific experience, visa or relocation need "
                   "the user's own answer: use the saved ones above, otherwise ask_user first. Karya won't type guesses.")
    return {k: v for k, v in out.items() if v not in (None, "")}


def _ats_details(url: str):
    from . import job_sources as S
    try:
        found = S.details(url)
    except requests.RequestException:
        raise
    except Exception:
        found = None
    if found:
        return found
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url)
    if m:
        d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}", {"questions": "true"})
        questions = [{"label": q.get("label"), "required": q.get("required"),
                      "type": (q.get("fields") or [{}])[0].get("type"),
                      "options": [v.get("label") for v in (q.get("fields") or [{}])[0].get("values") or []][:12] or None}
                     for q in d.get("questions") or []]
        return {"title": d.get("title"), "company": m.group(1), "location": (d.get("location") or {}).get("name"),
                "description": _clean_html(d.get("content"))[:6000], "form_questions": questions,
                "apply_url": d.get("absolute_url"), "how_to_apply": "Form is on the job page (no login). Use browser_open, browser_fill, browser_upload."}
    m = re.search(r"jobs\.lever\.co/([\w-]+)/([0-9a-f-]{36})", url)
    if m:
        d = _get_json(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
        lists = "\n".join(f"{x.get('text')}:\n{_clean_html(x.get('content'))}" for x in d.get("lists") or [])
        return {"title": d.get("text"), "company": m.group(1), "location": (d.get("categories") or {}).get("location"),
                "description": ((d.get("descriptionPlain") or "") + "\n" + lists + "\n" + (d.get("additionalPlain") or ""))[:6000],
                "apply_url": d.get("applyUrl") or (d.get("hostedUrl", "") + "/apply"), "how_to_apply": "Lever form (no login)."}
    m = re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", url)
    if m:
        for j in _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}").get("jobs", []):
            if j.get("id") == m.group(2) or m.group(2) in (j.get("jobUrl") or ""):
                return {"title": j.get("title"), "company": m.group(1), "location": j.get("location"),
                        "description": (j.get("descriptionPlain") or "")[:6000],
                        "apply_url": j.get("applyUrl") or (j.get("jobUrl", "") + "/application"), "how_to_apply": "Ashby form (no login)."}
    m = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", url)
    if m:
        d = _get_json(f"https://hn.algolia.com/api/v1/items/{m.group(1)}")
        return {"source": "Hacker News Who's Hiring", "author": d.get("author"), "posted": d.get("created_at"),
                "description": _clean_html((d.get("text") or "").replace("<p>", "\n"))[:6000],
                "how_to_apply": "Follow the instructions in the post (usually an email or an apply link)."}
    return None


_DETAILS_CACHE: dict[str, tuple[float, object]] = {}
DETAILS_TTL = 30 * 60


@tool("get_job_details", "Read a job posting in full: description, requirements, the application form's questions "
      "(Greenhouse) and how to apply. Pass the job's id from find_jobs (J1, J2...) or a URL.", {
    "job_id": P("string", "Job id from find_jobs, e.g. J2 (preferred)"),
    "url": P("string", "Job posting URL (only if there's no id)"),
}, group="jobs", precheck=picked_only)
def get_job_details(url: str = "", job_id: str = ""):
    url = resolve_job_url(job_id, url)
    if not url:
        return "ERROR: unknown job id. Use an id from the latest find_jobs result (J1, J2...)."
    cached = _DETAILS_CACHE.get(url)
    if cached and time.time() - cached[0] < DETAILS_TTL:
        out = json.loads(json.dumps(cached[1]))  # a copy
        if isinstance(out, dict) and job_id:
            out["job_id"] = job_id.upper()
        return out
    out = _job_details(url, job_id)
    if isinstance(out, dict) and (out.get("description") or out.get("text") or out.get("title")):
        _DETAILS_CACHE[url] = (time.time(), out)
    return out


def _job_details(url: str, job_id: str = ""):
    try:
        details = _ats_details(url)
    except requests.RequestException:
        details = None  # the posting API failed; read the page instead
    if details:
        if job_id:
            details["job_id"] = job_id.upper()
        return details
    match = re.search(r"linkedin\.com/.*?(\d{8,})", url)
    if match:
        resp = requests.get(LINKEDIN_POSTING.format(id=match.group(1)), headers=HEADERS, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        criteria = {_txt(li.select_one(".description__job-criteria-subheader")): _txt(li.select_one(".description__job-criteria-text"))
                    for li in soup.select(".description__job-criteria-item")}
        desc = soup.select_one(".show-more-less-html__markup")
        offsite = soup.select_one("code#applyUrl")
        apply_url = re.search(r'"(https?://[^"]+)"', str(offsite)).group(1) if offsite and re.search(r'"(https?://[^"]+)"', str(offsite)) else None
        return {"title": _txt(soup.select_one(".top-card-layout__title")),
                "company": _txt(soup.select_one(".topcard__org-name-link")),
                "location": _txt(soup.select_one(".topcard__flavor--bullet")), "criteria": criteria,
                "applicants": _txt(soup.select_one(".num-applicants__caption")),
                "description": (desc.get_text("\n", strip=True) if desc else "")[:6000], "url": url,
                "apply_url": apply_url, "how_to_apply": "Easy Apply needs a LinkedIn login (list_accounts / request_credentials)."}
    from .web import fetch_url
    page = fetch_url(url, max_chars=6000)
    if "ycombinator.com" in url:
        page["how_to_apply"] = ("YC jobs apply through Work at a Startup (free account at workatastartup.com): "
                                "list_accounts, else request_credentials or create an account with vault_new_password.")
    return page


# ----------------------------------------------------------------- freelance
@tool("search_freelance", "Find freelance projects/gigs (Freelancer.com live projects + Upwork job posts).", {
    "query": P("string", "Skill or project type, e.g. 'website design', 'shopify', 'wordpress', 'logo'"),
    "limit": P("integer", "Max results per source (default 8)"),
}, required=["query"], group="jobs")
def search_freelance(query: str, limit: int = 8):
    rows, errors = [], []
    try:
        data = requests.get("https://www.freelancer.com/api/projects/0.1/projects/active/",
                            params={"query": query, "limit": limit, "full_description": "false",
                                    "sort_field": "time_updated"}, headers=HEADERS, timeout=20).json()
        for p in data.get("result", {}).get("projects", []):
            budget = p.get("budget") or {}
            cur = (p.get("currency") or {}).get("code", "") if isinstance(p.get("currency"), dict) else ""
            stats = p.get("bid_stats") or {}
            rows.append({"source": "Freelancer", "title": p.get("title"), "type": p.get("type"),
                         "budget": f"{budget.get('minimum')}-{budget.get('maximum')} {cur}",
                         "bids": stats.get("bid_count"), "avg_bid": round(stats.get("bid_avg") or 0, 1),
                         "posted": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.get("time_submitted") or 0)),
                         "description": (p.get("preview_description") or "")[:220],
                         "url": f"https://www.freelancer.com/projects/{p.get('seo_url')}"})
    except Exception as exc:
        errors.append(f"freelancer: {type(exc).__name__}")
    try:
        for r in DDGS().text(f"site:upwork.com/freelance-jobs/apply {query}", max_results=limit):
            rows.append({"source": "Upwork", "title": r.get("title"), "description": (r.get("body") or "")[:220],
                         "url": r.get("href")})
    except Exception as exc:
        if "No results" not in str(exc):
            errors.append(f"upwork: {type(exc).__name__}")
    return {"count": len(rows), "projects": rows, "errors": errors,
            "tip": "Bidding needs a logged-in account: list_accounts / request_credentials, then browser_open the URL."}


# ----------------------------------------------------------------- resume + tracker
_resume_cache: dict[str, str] = {}


@tool("read_resume", "Read the user's resume (PDF/DOCX/TXT). Uses RESUME_PATH from settings unless a path is given.", {
    "path": P("string", "Optional different resume file"),
}, group="jobs")
def read_resume(path: str | None = None):
    target = str(path or settings.resume_path)
    if target in _resume_cache:
        return _resume_cache[target]
    from .pc import extract_text
    text = extract_text(target, 20000)
    if not text.startswith("ERROR"):
        _resume_cache[target] = text
    return text


@tool("track_application", "Save a job/freelance application to the tracker (do this after every application).", {
    "company": P("string", "Company or client"),
    "role": P("string", "Job title / project"),
    "url": P("string", "Posting URL"),
    "status": P("string", "Status", enum=["saved", "applied", "interview", "offer", "rejected", "withdrawn"]),
    "method": P("string", "How: linkedin, email, company site, freelancer, upwork..."),
    "notes": P("string", "Anything useful: contact, salary, follow-up date"),
}, required=["company", "role"], group="jobs")
def track_application(company: str, role: str, url: str = "", status: str = "applied", method: str = "", notes: str = ""):
    data = applications_store.load()
    apps = data.setdefault("applications", [])
    same = lambda a: (url and a.get("url") == url) or (  # noqa: E731
        (a.get("company") or "").strip().lower() == company.strip().lower() and (a.get("role") or "").strip().lower() == role.strip().lower())
    for app in apps:
        if same(app):
            app["status"] = status or app.get("status")
            if notes and notes not in (app.get("notes") or ""):
                app["notes"] = (app.get("notes", "") + " | " + notes).strip(" |")
            if method and not app.get("method"):
                app["method"] = method
            app["updated"] = now()
            applications_store.save(data)
            return f"Updated application #{app['id']}: {app['role']} at {app['company']} ({app['status']})."
    app_id = max([a.get("id", 0) for a in apps] + [0]) + 1
    apps.append({"id": app_id, "company": company, "role": role, "url": url, "status": status,
                 "method": method, "notes": notes, "created": now(), "updated": now()})
    applications_store.save(data)
    return f"Saved application #{app_id}: {role} at {company} ({status})."


@tool("application_queue", "The jobs the user picked to apply to: status shows what's applied and what's next; skip marks a "
      "job you can't apply to (with the reason) and gives the next one; done marks a job the user says they applied "
      "to themselves.", {
    "action": P("string", "status, skip or done", enum=["status", "skip", "done"]),
    "job_id": P("string", "Job id, e.g. J4"),
    "reason": P("string", "Why it was skipped (shown to the user)"),
}, group="jobs")
def application_queue(action: str = "status", job_id: str = "", reason: str = ""):
    from .. import apply_queue
    if action in ("skip", "done"):
        if not job_id:
            return "ERROR: give job_id"
        if action == "skip" and re.search(r"\b(years?|yrs|experience|senior|seniority|fit|qualif|overqualif|underqualif|"
                                          r"skills? questions?|suggested skills?)\b", reason or "", re.I):
            return ("NOT SKIPPED: the user already chose to apply to this job. Experience requirements are the company's "
                    "call and suggested skills are optional, so apply anyway and answer the form truthfully. Skip only "
                    "when applying is impossible (closed posting, a login you don't have, a broken form).")
        if action == "skip" and _SECOND_GUESS.search(reason or "") and not _user_dropped(job_id):
            return ("NOT SKIPPED: the user picked this job themselves, so it isn't yours to drop for being in another "
                    "place or 'not what they want'. Apply to it. (If the user asked for something new, just do that: a "
                    "new find_jobs and choose_jobs replace this list.) Skip only when applying is impossible.")
        job = apply_queue.mark(job_id, "skipped" if action == "skip" else "applied",
                               (reason or "skipped") if action == "skip" else "you applied yourself")
        if not job:
            return f"ERROR: {job_id} isn't in the pick list. Call application_queue(action=\"status\")."
        if action == "done":
            track_application(job.get("company", ""), job.get("title", ""), job.get("url", ""), "applied",
                              method="you applied yourself")
    return apply_queue.status_text()


# Skip reasons that second-guess what the user picked (the AI once dropped a picked job "because the user wants Dubai").
_SECOND_GUESS = re.compile(r"\buser (wants|wanted|asked|prefers|said|is looking)|\binstead\b|\bnot (in|what|the|a) |"
                           r"\b(location|country|city|place)\b|\bbetter (match|fit|option)|\bnot relevant|"
                           r"\b(doesn'?t|does not|don'?t) (match|suit|fit)", re.I)


def _user_dropped(job_id: str) -> bool:
    """The user's own recent words drop this job ("skip Replit", "don't apply to J50")."""
    from .. import answers, apply_queue
    job = next((j for j in apply_queue.load() if str(j.get("id", "")).upper() == str(job_id).upper()), None)
    said = " ".join(answers.RECENT_USER[-2:]).lower()
    if not job or not re.search(r"\b(skip|don'?t|do not|not|remove|drop|leave|stop|cancel)\b", said):
        return False
    names = [str(job.get("id", "")).lower()] + [w for w in re.findall(r"[a-z0-9]{3,}", str(job.get("company", "")).lower())]
    return any(re.search(rf"\b{re.escape(n)}\b", said) for n in names if n)


@tool("ask_user", "Ask the user questions only they can answer (notice period, current/expected salary, gender, years of a "
      "specific experience, visa, relocation...) in a form card and wait for the answers. Answers are saved for future "
      "forms. Put all the questions of a form in one call.", {
    "questions": P("array", "The questions, worded as the form asks them; add options if it's a choice",
                   items={"type": "object", "properties": {"question": {"type": "string"},
                                                           "options": {"type": "array", "items": {"type": "string"}}}}),
    "reason": P("string", "One short line, e.g. 'Zeta's application asks these'"),
}, required=["questions"], group="core")
def ask_user(questions=None, reason: str = ""):
    return "ERROR: ask_user only works from the chat (the user answers in a card)."


@tool("update_application", "Update status/notes of a tracked application.", {
    "app_id": P("integer", "Application id"),
    "status": P("string", "New status", enum=["saved", "applied", "interview", "offer", "rejected", "withdrawn"]),
    "notes": P("string", "Notes to append"),
}, required=["app_id"], group="jobs")
def update_application(app_id: int, status: str | None = None, notes: str | None = None):
    data = applications_store.load()
    for app in data.get("applications", []):
        if app.get("id") == app_id:
            if status:
                app["status"] = status
            if notes:
                app["notes"] = (app.get("notes", "") + " | " + notes).strip(" |")
            app["updated"] = now()
            applications_store.save(data)
            return f"Updated #{app_id}: {app['role']} at {app['company']} -> {app['status']}"
    return f"ERROR: no application with id {app_id}"


def applications_today() -> int:
    """Applications recorded today (by Karya, an AI app, or the user)."""
    today = time.strftime("%Y-%m-%d")
    return sum(1 for a in applications_store.load().get("applications", [])
               if str(a.get("created", "")).startswith(today) and a.get("status") == "applied")


# ----------------------------------------------------------------- what the user already applied to
# The user, 2026-10-06: "it has to remember what companies it has applied and for next session to not go and do the
# same thing search same companies again". The tracker (kept on disk across sessions) is the memory: a job already
# applied to is never shown or submitted again, and companies applied to recently are left out of new searches.
APPLIED_STATUSES = ("applied", "interview", "offer", "rejected", "withdrawn")
APPLIED_COMPANY_DAYS = 60


def applied_memory(days: int = APPLIED_COMPANY_DAYS) -> dict:
    """From the tracker: {"jobs": {posting id or url: app}, "companies": {name key: app}, "boards": {careers board:
    app}, "days": days}. Companies and boards count only for applications in the last `days` days."""
    from .. import apply_queue
    from . import job_sources as S
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    jobs, companies, boards = {}, {}, {}
    for app in applications_store.load().get("applications", []):
        if app.get("status") not in APPLIED_STATUSES:
            continue
        url = str(app.get("url") or "").strip()
        if url:
            jobs[url.rstrip("/").lower()] = app
            key = apply_queue.posting_key(url)
            if key:
                jobs[key] = app
        if str(app.get("created") or "")[:10] < cutoff:
            continue
        name = S.norm_name(app.get("company") or "")
        if len(name) >= 2:
            companies[name] = app
        board = (S.parse_ats_url(url) or {}).get("key")
        if board:
            boards[board] = app
    return {"jobs": jobs, "companies": companies, "boards": boards, "days": days}


def applied_before(job: dict, memory: dict | None = None) -> dict | None:
    """The tracked application for this exact posting, if the user already applied to it."""
    from .. import apply_queue
    memory = memory or applied_memory()
    for url in (job.get("url"), job.get("apply")):
        if not url:
            continue
        key = apply_queue.posting_key(url)
        hit = (key and memory["jobs"].get(key)) or memory["jobs"].get(str(url).rstrip("/").lower())
        if hit:
            return hit
    return None


def applied_company(job: dict, memory: dict) -> dict | None:
    """The user's recent application at this job's company (same name, or the same careers board)."""
    from . import job_sources as S
    key = S.norm_name(job.get("company") or job.get("name") or "")
    if key and key in memory["companies"]:
        return memory["companies"][key]
    for url in (job.get("url"), job.get("apply"), job.get("careers")):
        board = (S.parse_ats_url(str(url or "")) or {}).get("key")
        if board and board in memory["boards"]:
            return memory["boards"][board]
    return None


def skip_applied(rows: list[dict], memory: dict, include_companies: bool = False, asked: str = "") -> tuple[list, dict]:
    """Leave out jobs the user already applied to, and jobs at companies they applied to recently (unless
    include_companies, or the request names that company). Returns (kept, {"jobs": n, "companies": {name: n}})."""
    from . import job_sources as S
    squashed = re.sub(r"[^a-z0-9]", "", (asked or "").lower())
    named = {k for k in memory["companies"] if len(k) >= 4 and k in squashed}
    kept, hidden = [], {"jobs": 0, "companies": {}}
    for row in rows:
        if applied_before(row, memory):
            hidden["jobs"] += 1
            continue
        app = None if include_companies else applied_company(row, memory)
        if app and S.norm_name(app.get("company") or "") not in named:
            name = app.get("company") or row.get("company") or "?"
            hidden["companies"][name] = hidden["companies"].get(name, 0) + 1
            continue
        kept.append(row)
    return kept, hidden


def hidden_note(hidden: dict, days: int = APPLIED_COMPANY_DAYS) -> str:
    parts = []
    if hidden.get("jobs"):
        parts.append(f"{hidden['jobs']} job(s) the user already applied to (never shown again)")
    companies = hidden.get("companies") or {}
    if companies:
        names = sorted(companies, key=lambda n: -companies[n])
        parts.append(f"{sum(companies.values())} job(s) at {len(names)} compan{'y' if len(names) == 1 else 'ies'} they "
                     f"applied to in the last {days} days ({', '.join(names[:8])}{', ...' if len(names) > 8 else ''}); "
                     "include_applied_companies=true shows those again")
    return ("Left out: " + "; ".join(parts) + ".") if parts else ""


@tool("list_applications", "List tracked job/freelance applications.", {
    "status": P("string", "Optional filter by status"),
}, group="jobs")
def list_applications(status: str | None = None):
    apps = applications_store.load().get("applications", [])
    if status:
        apps = [a for a in apps if a.get("status") == status]
    return apps or "No applications tracked yet."


@tool("delete_application", "Remove an application from the tracker.", {
    "app_id": P("integer", "Application id"),
}, required=["app_id"], risk=CONFIRM, group="jobs")
def delete_application(app_id: int):
    data = applications_store.load()
    before = len(data.get("applications", []))
    data["applications"] = [a for a in data.get("applications", []) if a.get("id") != app_id]
    applications_store.save(data)
    return "Deleted." if len(data["applications"]) < before else f"ERROR: no application with id {app_id}"
