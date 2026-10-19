"""Site APIs: teach Karya a website action once, then call it like an API instead of driving the browser with the AI.

How it works, in three steps, none of which needs the model:
  learn  - the site's own page is loaded for two example inputs; the request that changed with the input becomes a
           template with {slots}, and cookies/tokens become references to a local session store (never their values).
  call   - the slots are filled and the request is sent (plain HTTP, or a fetch inside a page of the site when the
           site blocks a plain call), and the JSON or HTML is turned into the fields that were picked.
  repair - if a saved request stops working (a rotated id, a moved field), it is learned again, and the new version is
           kept only after a replay succeeds; otherwise the call returns an error, never someone else's data.

Reads run on their own. Writes (anything that changes the site) are recorded once while Karya does the action in the
browser, and every replay needs the user's approval; an HTTP 2xx is never taken as proof on its own.

This file is browser-free and holds the logic and storage. The capture and the sending are passed in (karya/
site_api_run.py provides them with Playwright and the extension), so the templating and extraction are unit-tested
without a real site.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .config import DATA_DIR

STORE_DIR = DATA_DIR / "site_apis"
SPECS_DIR = STORE_DIR / "ops"
SESSION_FILE = STORE_DIR / "_sessions.json"      # cookie/token VALUES, by domain (data/ is gitignored)
MAX_BODY = 2_000_000

# Headers that carry a secret or a per-session token: never stored as a value, kept as a reference and resolved at call
# time from the session store (or from the live page, for a token the page issues).
_SECRET_HEADERS = re.compile(r"^(cookie|authorization|proxy-authorization|x-csrf|x-xsrf|x-csrf-token|x-xsrf-token|"
                             r"csrf-token|x-api-key|api-key|x-auth-token|x-amz-security-token|x-goog-|x-sid|"
                             r"x-requested-with|x-guest-token|x-client-|authtoken|x-access-token)", re.I)
# Headers worth keeping on the template (they shape the response), minus anything secret above.
_KEEP_HEADERS = re.compile(r"^(content-type|accept|accept-language|x-requested-with|referer|origin|"
                           r"apollographql-|x-apollo-|graphql-|x-algolia-|x-restli-protocol-version|"
                           r"x-li-|user-agent|x-ig-|sec-ch-ua)", re.I)
# Hosts whose requests are noise (analytics, ads, fonts): never the operation's own call.
_NOISE_HOST = re.compile(r"(google-analytics|googletagmanager|doubleclick|facebook\.com/tr|hotjar|segment\.(io|com)|"
                         r"sentry\.io|datadog|mixpanel|amplitude|fullstory|clarity\.ms|optimizely|"
                         r"gstatic\.com|fonts\.googleapis|cdn\.jsdelivr|unpkg\.com|/favicon)", re.I)
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")[:60] or "site"


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().lstrip(".")


def registrable(host: str) -> str:
    """A rough registrable domain (last two labels), so www.x.com and api.x.com share one session and lock."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # common two-part public suffixes (enough for the sites Karya uses)
    two = {"co.uk", "co.in", "com.au", "co.jp", "com.br", "co.nz", "org.uk", "ac.in", "gov.in"}
    return ".".join(parts[-3:]) if ".".join(parts[-2:]) in two else ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    ra, rb = registrable(domain_of(a) if "/" in a or "." in a else a), registrable(domain_of(b) if "/" in b or "." in b else b)
    return bool(ra) and ra == rb


# ---------------------------------------------------------------- the record of one request the page made
@dataclass
class Record:
    method: str
    url: str
    request_headers: dict = field(default_factory=dict)
    request_body: str | None = None
    status: int = 0
    response_headers: dict = field(default_factory=dict)
    response_body: str = ""
    resource_type: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        return cls(method=str(d.get("method", "GET")).upper(), url=str(d.get("url", "")),
                   request_headers={str(k).lower(): str(v) for k, v in (d.get("request_headers") or {}).items()},
                   request_body=d.get("request_body"),
                   status=int(d.get("status") or 0),
                   response_headers={str(k).lower(): str(v) for k, v in (d.get("response_headers") or {}).items()},
                   response_body=str(d.get("response_body") or "")[:MAX_BODY],
                   resource_type=str(d.get("resource_type") or ""))

    def content_type(self) -> str:
        return self.response_headers.get("content-type", "")

    def looks_json(self) -> bool:
        body = self.response_body.lstrip()[:1]
        return "json" in self.content_type().lower() or body in "{["


# ---------------------------------------------------------------- learning a template from two examples
def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _slotify(text: str, inputs: dict) -> tuple[str, set[str]]:
    """Replace each input VALUE in the text with {param}. Returns the template and which params were found.
    Longer values first, so "New York" is slotted before "York"."""
    found: set[str] = set()
    for name in sorted(inputs, key=lambda k: -len(_as_text(inputs[k]))):
        val = _as_text(inputs[name])
        if val and val in text:
            text = text.replace(val, "{" + name + "}")
            found.add(name)
        else:                       # URL-encoding ("New York" -> "New%20York" / "New+York")
            for enc in (val.replace(" ", "%20"), val.replace(" ", "+")):
                if enc and enc != val and enc in text:
                    text = text.replace(enc, "{" + name + "}")
                    found.add(name)
                    break
    return text, found


def _candidate(records: list[Record], inputs: dict, want_write: bool) -> list[Record]:
    """Requests that carry the input values and could be THE operation call, best first."""
    out = []
    for r in records:
        host = domain_of(r.url)
        if not host or _NOISE_HOST.search(r.url) or r.resource_type in ("image", "font", "stylesheet", "media"):
            continue
        hay = r.url + " " + (r.request_body or "")
        if not all(_as_text(v) and (_as_text(v) in hay or _as_text(v).replace(" ", "%20") in hay or
                                    _as_text(v).replace(" ", "+") in hay) for v in inputs.values()):
            continue
        is_write = r.method in _WRITE_METHODS
        if want_write and not is_write:
            continue
        score = 0
        if r.looks_json():
            score += 2
        if r.resource_type in ("xhr", "fetch"):
            score += 2
        if r.method == "GET" and not want_write:
            score += 1
        if "/api" in r.url or "graphql" in r.url.lower():
            score += 1
        out.append((score, len(r.response_body), r))
    return [r for _, _, r in sorted(out, key=lambda t: (-t[0], -t[1]))]


def _template_from(r1: Record, r2: Record, in1: dict, in2: dict) -> dict | None:
    """One template that fits BOTH example requests: the slots sit where the inputs differ."""
    if r1.method != r2.method:
        return None
    url1, f1u = _slotify(r1.url, in1)
    url2, f2u = _slotify(r2.url, in2)
    body1, f1b = _slotify(r1.request_body or "", in1)
    body2, f2b = _slotify(r2.request_body or "", in2)
    if url1 != url2 or body1 != body2:
        return None                               # the fixed parts must match once the inputs are slotted out
    found = f1u | f1b
    if found != set(in1) or (f1u | f1b) != (f2u | f2b):
        return None                               # every input must map to a slot, the same way in both examples
    return {"method": r1.method, "url_template": url1, "body_template": (body1 or None) if r1.request_body else None}


def _split_headers(headers: dict) -> tuple[dict, list[dict]]:
    """Keep the shaping headers; turn secret/session ones into references resolved at call time."""
    keep, creds = {}, []
    for name, value in headers.items():
        low = name.lower()
        if low in ("content-length", "host", "connection", "accept-encoding"):
            continue
        if _SECRET_HEADERS.match(low):
            if low == "cookie":
                for cookie in re.findall(r"([^=;\s]+)=", value):
                    creds.append({"type": "cookie", "name": cookie})
            else:
                creds.append({"type": "header", "name": name})
            continue
        if _KEEP_HEADERS.match(low):
            keep[name] = value
    # one cookie reference per name
    seen, uniq = set(), []
    for c in creds:
        key = (c["type"], c["name"].lower())
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return keep, uniq


def learn_operation(site: str, name: str, examples: list[dict], extract: dict | None = None,
                    kind: str = "read", pick: int | None = None) -> dict:
    """Build an operation spec from two (or more) example captures.

    examples: [{"inputs": {param: value}, "records": [record dicts]}, ...]
    extract:  how to read the response - {"json": "path.to.items", "fields": {...}} or
              {"html": "li.row", "fields": {"title": "a", "url": "a@href"}}; omitted for a write.
    pick:     index of the request to template (when the caller already chose); otherwise the best candidate is used.
    """
    if len(examples) < 2:
        raise ValueError("learning needs two example inputs, so the changing part can be found")
    ex1, ex2 = examples[0], examples[1]
    recs1 = [Record.from_dict(r) for r in ex1["records"]]
    recs2 = [Record.from_dict(r) for r in ex2["records"]]
    want_write = kind == "write"
    cands1 = [recs1[pick]] if pick is not None else _candidate(recs1, ex1["inputs"], want_write)
    if not cands1:
        raise LearnError("no request on the page carried the example input. Give the page where the action happens, "
                         "or an input that really appears in what the site sends.")
    spec_core = None
    chosen = None
    for c1 in cands1:
        for c2 in _candidate(recs2, ex2["inputs"], want_write):
            if same_site(c1.url, c2.url):
                tpl = _template_from(c1, c2, ex1["inputs"], ex2["inputs"])
                if tpl:
                    spec_core, chosen = tpl, c1
                    break
        if spec_core:
            break
    if not spec_core:
        raise LearnError("couldn't match the two examples to one request. The site may send the input in a way Karya "
                         "can't see (hashed, or in a websocket); use the browser for this one.")
    keep, creds = _split_headers(chosen.request_headers)
    spec = {
        "site": _slug(site),
        "operation": _slug(name),
        "domain": registrable(domain_of(chosen.url)),
        "kind": "write" if want_write else "read",
        **spec_core,
        "headers": keep,
        "credentials": creds,
        "params": sorted(set(ex1["inputs"])),
        "extract": extract or ({} if want_write else _guess_extract(chosen)),
        "example_inputs": [ex1["inputs"], ex2["inputs"]],
        "sample_status": chosen.status,
        "learned_at": time.time(),
    }
    return spec


def _guess_extract(r: Record) -> dict:
    """A default read extraction: the largest list of objects in the JSON (so a search returns its rows)."""
    if not r.looks_json():
        return {"text": True}
    try:
        data = json.loads(r.response_body)
    except (ValueError, TypeError):
        return {"text": True}
    best = _largest_list(data, "")
    return {"json": best} if best else {"json": ""}


def _largest_list(node, path: str, depth: int = 0):
    best, best_len = (path if isinstance(node, list) else None), (len(node) if isinstance(node, list) else -1)
    if depth < 6 and isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            cand = _largest_list(v, p, depth + 1)
            if cand is not None:
                try:
                    length = len(_json_path(node if path == "" else {k: v}, cand[len(path):].lstrip(".") if path else cand))
                except Exception:  # noqa: BLE001
                    length = 0
                if length > best_len:
                    best, best_len = cand, length
    return best


def learn_from_single(site: str, name: str, records: list, inputs: dict, extract: dict | None = None,
                      kind: str = "write", pick: int | None = None) -> dict:
    """Template an operation from ONE recording (a write Karya did once in the browser) plus the values it entered.
    The values must appear in the request, so the slots sit where those values were. Used for actions that change the
    site, where two example runs aren't wanted."""
    recs = [Record.from_dict(r) for r in records]
    want_write = kind == "write"
    cands = [recs[pick]] if pick is not None else _candidate(recs, inputs, want_write)
    if not cands:
        raise LearnError("none of the recorded requests carried the values you entered, so there's nothing to replay. "
                         "Make sure the action really sent those values (it may use a websocket, or hash them).")
    chosen = cands[0]
    url_t, fu = _slotify(chosen.url, inputs)
    body_t, fb = _slotify(chosen.request_body or "", inputs)
    if (fu | fb) != set(inputs):
        missing = set(inputs) - (fu | fb)
        raise LearnError(f"couldn't find {', '.join(sorted(missing))} in the request, so it can't be turned into an "
                         "input. Record the action again, or drop those inputs.")
    keep, creds = _split_headers(chosen.request_headers)
    spec = {
        "site": _slug(site), "operation": _slug(name), "domain": registrable(domain_of(chosen.url)),
        "kind": "write" if want_write else "read", "method": chosen.method, "url_template": url_t,
        "body_template": (body_t or None) if chosen.request_body else None, "headers": keep, "credentials": creds,
        "params": sorted(set(inputs)), "extract": extract or {"text": True},
        "success": _success_hint(chosen), "example_inputs": [inputs], "sample_status": chosen.status,
        "learned_at": time.time(),
    }
    return spec


def _success_hint(r: Record) -> dict:
    """What a successful replay looks like, so a 2xx alone is never taken as done. Kept from the one recording that
    worked: its status, and a short marker from the response body."""
    body = (r.response_body or "").strip()
    marker = ""
    if r.looks_json():
        for key in ("success", "ok", "status", "id", "data"):
            if re.search(rf'"{key}"\s*:', body):
                marker = key
                break
    return {"status": r.status, "json_key": marker}


def write_succeeded(spec: dict, status: int, body: str) -> bool:
    """True only when a write replay matches what the recorded success looked like (not merely HTTP 2xx)."""
    hint = spec.get("success") or {}
    if not (200 <= status < 300):
        return False
    if hint.get("status") and abs(status - int(hint["status"])) > 1 and status != int(hint["status"]):
        return False
    key = hint.get("json_key")
    if key:
        if not re.search(rf'"{re.escape(key)}"\s*:', body or ""):
            return False
        if re.search(rf'"{re.escape(key)}"\s*:\s*(false|null|0|"?(error|failed|denied)"?)', body or "", re.I):
            return False
    return True


class LearnError(RuntimeError):
    """Learning couldn't produce a template (the input isn't in the request, or the two examples didn't match)."""


# ---------------------------------------------------------------- filling a template for a call
def _fill(template: str, inputs: dict) -> str:
    def repl(m):
        name = m.group(1)
        if name not in inputs:
            raise KeyError(name)
        from urllib.parse import quote
        val = _as_text(inputs[name])
        return quote(val, safe="") if ("=" + "{" + name + "}") in template or ("/" + "{" + name + "}") in template else val
    # keep it simple and safe: URL-encode values going into a URL, leave body values as-is (JSON re-encoded below)
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", repl, template)


def prepare(spec: dict, inputs: dict) -> dict:
    """The concrete request to send: method, url, headers, body. Raises KeyError for a missing input."""
    missing = [p for p in spec.get("params", []) if p not in inputs]
    if missing:
        raise KeyError(missing[0])
    url = _fill_url(spec["url_template"], inputs)
    body = None
    if spec.get("body_template"):
        body = _fill_body(spec["body_template"], inputs)
    headers = dict(spec.get("headers") or {})
    return {"method": spec["method"], "url": url, "headers": headers, "body": body}


def _fill_url(template: str, inputs: dict) -> str:
    from urllib.parse import quote
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", lambda m: quote(_as_text(inputs[m.group(1)]), safe=""), template)


def _fill_body(template: str, inputs: dict) -> str:
    # In a JSON body the value must be JSON-escaped; elsewhere inserted as-is.
    def repl(m):
        val = _as_text(inputs[m.group(1)])
        return json.dumps(val)[1:-1]            # escape for inside a JSON string
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", repl, template)


# ---------------------------------------------------------------- reading the response
def extract(spec: dict, body: str, content_type: str = "") -> object:
    rule = spec.get("extract") or {}
    if rule.get("text") or (not rule):
        return body[:8000]
    if "json" in rule:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return {"error": "the response wasn't JSON", "sample": body[:500]}
        rows = _json_path(data, rule["json"])
        fields = rule.get("fields")
        if isinstance(rows, list) and fields:
            return [{k: _json_path(item, p) for k, p in fields.items()} for item in rows]
        return rows
    if "html" in rule:
        return _extract_html(body, rule["html"], rule.get("fields") or {})
    return body[:8000]


def _json_path(data, path: str):
    """A dotted path with [] for 'each element of a list': "data.hits" or "results[].name"."""
    if path in ("", None):
        return data
    cur = data
    for part in re.split(r"\.(?![^\[]*\])", path):
        m = re.match(r"^([^\[]*)(\[\])?$", part)
        key, is_list = m.group(1), bool(m.group(2))
        if key:
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list):
                cur = [x.get(key) if isinstance(x, dict) else None for x in cur]
            else:
                return None
        if is_list and not isinstance(cur, list):
            cur = [cur] if cur is not None else []
    return cur


def _extract_html(html: str, item_sel: str, fields: dict) -> list:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for node in soup.select(item_sel):
        if fields:
            row = {}
            for key, sel in fields.items():
                target, _, attr = sel.partition("@")
                el = node.select_one(target) if target else node
                if el is None:
                    row[key] = None
                elif attr:
                    row[key] = el.get(attr)
                else:
                    row[key] = el.get_text(" ", strip=True)
            out.append(row)
        else:
            out.append(node.get_text(" ", strip=True))
    return out


# ---------------------------------------------------------------- storage
def _site_file(site: str) -> Path:
    return SPECS_DIR / f"{_slug(site)}.json"


def load_site(site: str) -> dict:
    try:
        return json.loads(_site_file(site).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_spec(spec: dict) -> dict:
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    site = load_site(spec["site"]) or {"site": spec["site"], "operations": {}, "domains": []}
    site["operations"][spec["operation"]] = spec
    if spec["domain"] and spec["domain"] not in site["domains"]:
        site["domains"].append(spec["domain"])
    _site_file(spec["site"]).write_text(json.dumps(site, ensure_ascii=False, indent=1), encoding="utf-8")
    return spec


def get_spec(site: str, operation: str) -> dict | None:
    return (load_site(site).get("operations") or {}).get(_slug(operation))


def all_sites() -> list[dict]:
    out = []
    for f in sorted(SPECS_DIR.glob("*.json")) if SPECS_DIR.exists() else []:
        try:
            site = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(site)
    return out


def forget(site: str, operation: str | None = None) -> bool:
    f = _site_file(site)
    if operation is None:
        existed = f.exists()
        f.unlink(missing_ok=True)
        return existed
    data = load_site(site)
    if data.get("operations", {}).pop(_slug(operation), None) is not None:
        f.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    return False


# ---------------------------------------------------------------- sessions (cookie/token values, by domain)
def load_sessions() -> dict:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_session(domain: str, cookies: dict, tokens: dict | None = None) -> None:
    domain = registrable(domain)
    if not domain:
        return
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    data = load_sessions()
    entry = data.setdefault(domain, {"cookies": {}, "tokens": {}})
    entry["cookies"].update({str(k): str(v) for k, v in (cookies or {}).items()})
    entry["tokens"].update({str(k): str(v) for k, v in (tokens or {}).items()})
    entry["updated"] = time.time()
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def remember_session(records: list, spec: dict) -> None:
    """Save the cookie and token VALUES the examples carried, for the operation's domain, into the session store
    (gitignored). The spec keeps only references; this is where the values live."""
    recs = [r if isinstance(r, Record) else Record.from_dict(r) for r in records]
    names = {c["name"] for c in spec.get("credentials") or [] if c["type"] == "cookie"}
    header_refs = {c["name"].lower() for c in spec.get("credentials") or [] if c["type"] == "header"}
    cookies, tokens = {}, {}
    for r in recs:
        if not same_site(r.url, spec.get("domain") or r.url) and registrable(domain_of(r.url)) != spec.get("domain"):
            continue
        for k, v in re.findall(r"([^=;\s]+)=([^;]*)", r.request_headers.get("cookie", "")):
            if not names or k in names:
                cookies[k] = v
        for name, val in r.request_headers.items():
            if name.lower() in header_refs:
                tokens[name] = val
    if cookies or tokens:
        save_session(spec.get("domain") or "", cookies, tokens)


# A response that isn't the data but a wall: a login page, a block, or a bot challenge.
_CHALLENGE = re.compile(r"(cf-chl|/cdn-cgi/challenge|captcha|recaptcha|hcaptcha|are you a human|unusual traffic|"
                        r"please enable javascript and cookies|access denied|log ?in to continue|sign ?in to continue)", re.I)


def response_class(status: int, body: str, content_type: str) -> str:
    """ok, auth (needs a login/session), blocked (challenge/anti-bot), or http (other error)."""
    if status in (401, 403) or (status == 200 and _CHALLENGE.search(body[:4000] or "")):
        return "blocked" if _CHALLENGE.search(body[:4000] or "") else "auth"
    if status == 429:
        return "blocked"
    if status >= 400 or status == 0:
        return "http"
    return "ok"


def resolve_headers(spec: dict) -> tuple[dict, list[str]]:
    """The headers to send (template headers + resolved cookies/tokens), and which references are still missing."""
    headers = dict(spec.get("headers") or {})
    session = load_sessions().get(spec.get("domain"), {})
    cookies = session.get("cookies") or {}
    tokens = session.get("tokens") or {}
    cookie_parts, missing = [], []
    for ref in spec.get("credentials") or []:
        if ref["type"] == "cookie":
            if ref["name"] in cookies:
                cookie_parts.append(f"{ref['name']}={cookies[ref['name']]}")
            else:
                missing.append(f"cookie {ref['name']}")
        elif ref["type"] == "header":
            val = tokens.get(ref["name"]) or tokens.get(ref["name"].lower())
            if val:
                headers[ref["name"]] = val
            else:
                missing.append(f"header {ref['name']}")
    if cookie_parts:
        headers["Cookie"] = "; ".join(cookie_parts)
    return headers, missing


def needs_credentials(spec: dict) -> bool:
    return bool(spec.get("credentials"))
