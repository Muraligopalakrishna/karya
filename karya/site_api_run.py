"""Running a site API: capture a page's requests, send a learned request, and heal it when the site changes.

site_api.py holds the browser-free logic; this file does the parts that touch the network and a browser. Capture uses
a headless Chrome (Playwright) by default, or the user's own Chrome through the extension when the site needs their
login. Sending tries, in order:
  tier 1  plain HTTP with Node-free `requests` (fast, cheap, no browser);
  tier 2  a fetch inside a page of the site (the browser's own cookies go with it, nothing is exported);
and for a read only, tier 3 would be running the page itself - not needed yet.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from . import site_api as S
from .config import settings

CAPTURE_WAIT_MS = 3500
CAPTURE_TYPES = {"xhr", "fetch", "document", "script"}


# ---------------------------------------------------------------- capture (headless Chrome)
def using_extension() -> bool:
    from .ext_link import link
    return settings.browser_mode != "karya" and link.connected


def capture_page(url: str, extra_headers: dict | None = None, wait_ms: int = CAPTURE_WAIT_MS,
                 logged_in: bool = False, prefer_extension: bool = True) -> list[dict]:
    """Load the page and record the requests it makes (method, url, headers, body, response). Uses the user's own
    Chrome through the extension when it's connected (so their logins apply); otherwise a headless Chrome. logged_in
    uses Karya's own browser profile, which may already be signed in to the site."""
    if prefer_extension and using_extension():
        from .ext_link import link
        try:
            res = link.request("site_capture", {"url": url, "wait_ms": wait_ms}, timeout=max(30.0, wait_ms / 1000 + 25))
            if isinstance(res, dict) and res.get("ok"):
                return res.get("records") or []
        except RuntimeError:
            pass   # fall back to a headless Chrome
    from playwright.sync_api import sync_playwright
    records: list[dict] = []
    pending: dict = {}
    with sync_playwright() as pw:
        launch = _launch_kwargs()
        if logged_in:
            ctx = pw.chromium.launch_persistent_context(str(settings.profile_dir if hasattr(settings, "profile_dir")
                                                             else S.DATA_DIR / "browser_profile"), **launch)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            browser = None
        else:
            browser = pw.chromium.launch(**{k: v for k, v in launch.items() if k not in ("user_data_dir",)})
            ctx = browser.new_context()
            page = ctx.new_page()
        if extra_headers:
            ctx.set_extra_http_headers({k: str(v) for k, v in extra_headers.items()})

        def on_request_finished(req):
            try:
                resp = req.response()
                body, ctype = "", ""
                if resp is not None:
                    ctype = (resp.headers or {}).get("content-type", "")
                    if req.resource_type in ("xhr", "fetch", "document") or "json" in ctype:
                        try:
                            body = resp.text()
                        except Exception:  # noqa: BLE001 - binary or already consumed
                            body = ""
                records.append({
                    "method": req.method, "url": req.url,
                    "request_headers": dict(req.headers or {}),
                    "request_body": req.post_data,
                    "status": resp.status if resp is not None else 0,
                    "response_headers": dict(resp.headers or {}) if resp is not None else {},
                    "response_body": body[:S.MAX_BODY], "resource_type": req.resource_type,
                })
            except Exception:  # noqa: BLE001 - one request failing never loses the rest
                pass

        page.on("requestfinished", on_request_finished)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(wait_ms)
        except Exception as exc:  # noqa: BLE001
            records.append({"method": "GET", "url": url, "status": 0, "response_body": f"[load error: {exc}]"})
        finally:
            try:
                (browser or ctx).close()
            except Exception:  # noqa: BLE001
                pass
    return records


def _launch_kwargs() -> dict:
    kw = {"headless": True}
    channel = getattr(settings, "browser_channel", "chrome")
    if channel:
        kw["channel"] = channel
    return kw


# ---------------------------------------------------------------- send (tier 1: plain HTTP)
def http_send(prepared: dict, timeout: float = 25.0) -> dict:
    import requests
    try:
        resp = requests.request(prepared["method"], prepared["url"], headers=prepared.get("headers") or {},
                                data=(prepared.get("body") or None), timeout=timeout, allow_redirects=True)
        body = resp.text[:S.MAX_BODY]
        return {"status": resp.status_code, "headers": dict(resp.headers), "body": body,
                "content_type": resp.headers.get("content-type", "")}
    except requests.RequestException as exc:
        return {"status": 0, "headers": {}, "body": f"[request error: {exc}]", "content_type": "", "error": str(exc)}


# ---------------------------------------------------------------- send (tier 2: fetch inside a page of the site)
def page_fetch(prepared: dict, logged_in: bool = True) -> dict:
    """Run the request with fetch() inside a real page on the same site, so the browser attaches its own cookies and
    the site sees a normal same-origin call. Reads only, unless the caller already has the user's approval."""
    from playwright.sync_api import sync_playwright
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(prepared["url"]))
    with sync_playwright() as pw:
        launch = _launch_kwargs()
        ctx = pw.chromium.launch_persistent_context(str(S.DATA_DIR / "browser_profile"), **launch)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(origin + "/", wait_until="domcontentloaded", timeout=30000)
            result = page.evaluate(
                """async ({url, method, headers, body}) => {
                    try {
                        const r = await fetch(url, {method, headers, body: body || undefined, credentials: 'include'});
                        const text = await r.text();
                        const h = {}; r.headers.forEach((v, k) => h[k] = v);
                        return {status: r.status, headers: h, body: text.slice(0, 2000000)};
                    } catch (e) { return {status: 0, headers: {}, body: '[fetch error: ' + e + ']'}; }
                }""",
                {"url": prepared["url"], "method": prepared["method"],
                 "headers": prepared.get("headers") or {}, "body": prepared.get("body")})
            result["content_type"] = (result.get("headers") or {}).get("content-type", "")
            return result
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- one call, with retry / fallback / repair
def call(site: str, operation: str, inputs: dict, allow_write: bool = False, transport=None, relearn=None) -> dict:
    """Run a learned operation. transport/relearn are injected in tests; by default HTTP then an in-page fetch, and a
    re-learn through capture. Returns {ok, tier, class, data|reason, next, ms}."""
    started = time.time()
    spec = S.get_spec(site, operation)
    if not spec:
        return _fail("missing", f"no operation {operation!r} for {site!r}. Learn it first with site_api_learn.")
    if spec.get("kind") == "write" and not allow_write:
        return _fail("needs_approval", "this is a write (it changes the site). Karya asks the user before each one.")
    try:
        prepared = S.prepare(spec, inputs)
    except KeyError as exc:
        return _fail("input", f"missing input {exc.args[0]!r}. This operation needs: {', '.join(spec['params'])}.")
    headers, missing = S.resolve_headers(spec)
    prepared["headers"] = {**prepared["headers"], **headers}
    send = transport or _default_transport

    is_write = spec.get("kind") == "write"
    attempt = send(prepared, tier=1)
    cls = S.response_class(attempt["status"], attempt.get("body", ""), attempt.get("content_type", ""))
    if cls == "ok":
        if is_write and not S.write_succeeded(spec, attempt["status"], attempt.get("body", "")):
            return {"ok": False, "tier": 1, "class": "unconfirmed", "status": attempt["status"],
                    "reason": "the request went through (HTTP %s) but the response didn't look like the recorded "
                              "success, so Karya won't say it worked. Check on the site." % attempt["status"],
                    "body": attempt.get("body", "")[:300], "ms": int((time.time() - started) * 1000)}
        return _done(spec, attempt, 1, started)

    # tier 2: a fetch inside a page of the site (its own cookies, fresh tokens)
    if cls in ("auth", "blocked") or (cls == "http" and S.needs_credentials(spec)):
        page = send(prepared, tier=2)
        pcls = S.response_class(page["status"], page.get("body", ""), page.get("content_type", ""))
        if pcls == "ok":
            if is_write and not S.write_succeeded(spec, page["status"], page.get("body", "")):
                return {"ok": False, "tier": 2, "class": "unconfirmed", "status": page["status"],
                        "reason": "sent, but the response didn't match the recorded success. Check on the site.",
                        "body": page.get("body", "")[:300], "ms": int((time.time() - started) * 1000)}
            return _done(spec, page, 2, started)
        attempt, cls = page, pcls

    # repair: the request may have changed (a rotated id, a moved field). Re-learn from the page, then replay once.
    if relearn and not is_write and cls in ("http", "ok"):
        healed = relearn(spec, inputs)
        if healed:
            S.save_spec(healed)
            again = send(S.prepare(healed, inputs) | {"headers": S.resolve_headers(healed)[0]}, tier=1)
            if S.response_class(again["status"], again.get("body", ""), again.get("content_type", "")) == "ok":
                return _done(healed, again, 1, started, healed=True)

    reasons = {"auth": ("auth", "the site wants a login. Run site_api_login for this site, or capture it from the "
                               "user's own Chrome."),
               "blocked": ("blocked", "the site served a challenge or a block (CAPTCHA / anti-bot). Use the browser "
                                      "for this one."),
               "http": ("changed", f"the request failed (HTTP {attempt['status']}). The site may have changed; "
                                    "re-learn it with site_api_learn.")}
    klass, why = reasons.get(cls, ("error", attempt.get("body", "")[:200]))
    return {"ok": False, "tier": 0, "class": klass, "reason": why, "status": attempt["status"],
            "ms": int((time.time() - started) * 1000)}


def ext_fetch(prepared: dict) -> dict:
    """tier 2 through the extension: the request runs in a page of the site in the user's own Chrome."""
    from .ext_link import link
    res = link.request("site_fetch", {"url": prepared["url"], "method": prepared["method"],
                                      "headers": prepared.get("headers") or {}, "body": prepared.get("body")},
                       timeout=45.0)
    if not isinstance(res, dict):
        return {"status": 0, "headers": {}, "body": "[no result]", "content_type": ""}
    res.setdefault("content_type", (res.get("headers") or {}).get("content-type", ""))
    return res


def _default_transport(prepared: dict, tier: int) -> dict:
    if tier == 1:
        return http_send(prepared)
    if using_extension():               # the user's Chrome, with their logins
        try:
            return ext_fetch(prepared)
        except RuntimeError:
            pass
    return page_fetch(prepared)


def _done(spec: dict, resp: dict, tier: int, started: float, healed: bool = False) -> dict:
    data = S.extract(spec, resp.get("body", ""), resp.get("content_type", ""))
    out = {"ok": True, "tier": tier, "class": "ok", "data": data, "status": resp["status"],
           "ms": int((time.time() - started) * 1000)}
    if healed:
        out["healed"] = True
    return out


def _fail(klass: str, reason: str) -> dict:
    return {"ok": False, "tier": 0, "class": klass, "reason": reason}
