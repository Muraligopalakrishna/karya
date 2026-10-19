"""Tools that let Karya teach itself a website once and then call it like an API, instead of driving the browser with
the model for every step. See karya/site_api.py for how learning, calling and repair work.

Reads (search, listings, details) are learned from two example inputs and run on their own. Writes (anything that
changes the site) are recorded once while Karya does the action in the browser, and every replay asks the user.
"""
from __future__ import annotations

import json

from .. import site_api as S
from .. import site_api_run as R
from ..registry import CRITICAL, P, tool

_RECORDING: dict = {}   # a write recording in progress: {site, operation, url}


def _outline(value, depth: int = 0, width: int = 6) -> object:
    """A small shape of a JSON value, so the agent can pick a path and fields without reading the whole response."""
    if isinstance(value, dict):
        return {k: _outline(v, depth + 1, width) for k, v in list(value.items())[:12]} if depth < 4 else "{...}"
    if isinstance(value, list):
        return [_outline(value[0], depth + 1, width)] + ([f"...{len(value)} items"] if len(value) > 1 else []) if value else []
    if isinstance(value, str):
        return (value[:60] + "...") if len(value) > 60 else value
    return value


@tool("site_api_scout", "Look at the requests a web page makes, so you can learn it as an API. Loads the page once and "
      "lists the JSON/data calls it sends (method, URL, and a small shape of what each returns) plus whether the page "
      "itself is HTML you could read. Use this before site_api_learn to choose the request and the fields.", {
    "url": P("string", "The page URL to inspect (a search results page, a listing, a profile...)"),
}, required=["url"], group="site_api")
def site_api_scout(url: str):
    records = R.capture_page(url, wait_ms=R.CAPTURE_WAIT_MS)
    out = []
    for rec in records:
        r = S.Record.from_dict(rec)
        host = S.domain_of(r.url)
        if not host or S._NOISE_HOST.search(r.url) or r.resource_type in ("image", "font", "stylesheet", "media"):
            continue
        row = {"method": r.method, "url": r.url[:200], "status": r.status, "type": r.resource_type}
        if r.looks_json():
            try:
                row["returns"] = _outline(json.loads(r.response_body))
            except (ValueError, TypeError):
                row["returns"] = "(unparseable JSON)"
        elif r.resource_type == "document":
            row["returns"] = "HTML page (use an html extract: a CSS selector for each row, 'sel@attr' for a link)"
        out.append(row)
    if not out:
        return {"url": url, "requests": [], "note": "The page made no data calls Karya could see. It may render on the "
                "server (then learn the page URL itself with an html extract) or need a login (open it in the browser "
                "first, with the extension connected)."}
    return {"url": url, "requests": out[:25], "next": "Pick the request that has the data, then call site_api_learn "
            "with a trigger URL that has {slots} for the inputs, two example inputs, and an extract."}


@tool("site_api_learn", "Teach Karya a read operation on a site (search, a listing, details) so it can be called like "
      "an API instead of using the browser. Give a trigger URL with {slots} for the inputs and two example inputs; "
      "Karya loads it for each, finds the request that changed, and saves a reusable operation. Reads only - use "
      "site_api_record for anything that changes the site.", {
    "site": P("string", "A short name for the site, e.g. 'hacker-news' or 'instahyre'"),
    "operation": P("string", "A short name for this action, e.g. 'search' or 'job_details'"),
    "trigger": P("string", "A URL with {slots}, e.g. https://site.com/search?q={query} (the page or the data URL)"),
    "example": P("object", "First example inputs, e.g. {\"query\": \"sqlite\"}"),
    "example2": P("object", "Second example inputs (different values), e.g. {\"query\": \"postgres\"}"),
    "extract": P("object", "How to read the result: {\"json\": \"path.to.list\", \"fields\": {\"title\": \"title\"}} or "
                           "{\"html\": \"li.row\", \"fields\": {\"title\": \"a\", \"url\": \"a@href\"}}. Omit to return raw."),
    "verify": P("object", "Optional third input to test the saved operation with (not one of the examples)"),
}, required=["site", "operation", "trigger", "example", "example2"], group="site_api")
def site_api_learn(site: str, operation: str, trigger: str, example: dict, example2: dict,
                   extract: dict | None = None, verify: dict | None = None):
    try:
        recs1 = R.capture_page(S._fill_url(trigger, example), wait_ms=R.CAPTURE_WAIT_MS)
        recs2 = R.capture_page(S._fill_url(trigger, example2), wait_ms=R.CAPTURE_WAIT_MS)
        spec = S.learn_operation(site, operation, [{"inputs": example, "records": recs1},
                                                   {"inputs": example2, "records": recs2}], extract=extract, kind="read")
    except S.LearnError as exc:
        return f"NOT LEARNED: {exc}"
    S.remember_session(recs1 + recs2, spec)
    S.save_spec(spec)
    test = R.call(site, operation, verify or example2)
    if not test.get("ok"):
        return {"learned": True, "operation": spec["operation"], "url_template": spec["url_template"],
                "warning": f"saved, but a test call failed ({test.get('class')}: {test.get('reason')}). It may need a "
                           "login, or the extract may be wrong. Check with site_api_call.", "params": spec["params"]}
    return {"learned": True, "site": spec["site"], "operation": spec["operation"], "params": spec["params"],
            "url_template": spec["url_template"], "needs_login": S.needs_credentials(spec), "tier": test.get("tier"),
            "sample": test.get("data") if not isinstance(test.get("data"), list) else test["data"][:3]}


@tool("site_api_record", "Start recording a site action you are about to do ONCE in the browser (apply, post, send), so "
      "Karya can repeat it as an API for the next items. Call this, then do the action with the browser tools, then "
      "call site_api_learn_write. The recording watches only Karya's current tab.", {
    "site": P("string", "The site name, e.g. 'instahyre'"),
    "operation": P("string", "A name for the action, e.g. 'apply'"),
}, required=["site", "operation"], group="site_api")
def site_api_record(site: str, operation: str):
    from ..ext_link import link
    if not R.using_extension() or not link.connected:
        return ("NOT STARTED: recording a write needs the Karya Browser Link extension connected to your Chrome. "
                "Connect it, or do this action with the browser tools without recording.")
    try:
        res = link.request("record_start", {}, timeout=20)
    except RuntimeError as exc:
        return f"NOT STARTED: {exc}"
    if not (isinstance(res, dict) and res.get("ok")):
        return f"NOT STARTED: {(res or {}).get('error', 'could not start recording')}"
    _RECORDING.update({"site": site, "operation": operation})
    return {"recording": True, "next": "Now do the action once in the browser (fill the form and submit, the user "
            "approves as usual). Then call site_api_learn_write with the values you entered that should become inputs."}


@tool("site_api_learn_write", "Finish recording a site action and save it as a write operation. Give the values you "
      "just entered that should become inputs (so next time Karya fills new ones). Replaying it always asks the user.", {
    "inputs": P("object", "The values you entered that are inputs, e.g. {\"jobId\": \"J99\", \"coverLetter\": \"...\"}. "
                          "Fixed values that are always the same can be left out."),
    "extract": P("object", "Optional: how to read the reply to confirm success"),
}, required=["inputs"], group="site_api")
def site_api_learn_write(inputs: dict, extract: dict | None = None):
    from ..ext_link import link
    if not _RECORDING:
        return "NOT SAVED: nothing is being recorded. Call site_api_record first, then do the action, then this."
    try:
        res = link.request("record_finish", {}, timeout=30)
    except RuntimeError as exc:
        return f"NOT SAVED: {exc}"
    records = (res or {}).get("records") if isinstance(res, dict) else None
    if not records:
        return "NOT SAVED: no requests were recorded. Did the action actually send to the site?"
    site, operation = _RECORDING["site"], _RECORDING["operation"]
    _RECORDING.clear()
    try:
        spec = S.learn_from_single(site, operation, records, inputs, extract=extract, kind="write")
    except S.LearnError as exc:
        return f"NOT SAVED: {exc}"
    S.remember_session(records, spec)
    S.save_spec(spec)
    return {"learned_write": True, "site": spec["site"], "operation": spec["operation"], "params": spec["params"],
            "note": "Saved. site_api_call will replay it for new inputs, and will ask you before each one. A 2xx reply "
                    "alone is never taken as success."}


def _call_risk(args) -> tuple:
    spec = S.get_spec(str(args.get("site", "")), str(args.get("operation", "")))
    if spec and spec.get("kind") == "write":
        return CRITICAL, (f"Replay the '{args.get('operation')}' action on {spec.get('domain')} with "
                          f"{json.dumps(args.get('inputs') or {}, ensure_ascii=False)[:200]} (this changes the site)")
    return "safe", ""


@tool("site_api_call", "Call an operation Karya already learned (see site_api_list), instead of using the browser. "
      "Fast and cheap: a read returns JSON. A write asks you first and is only reported done if the site confirms it.", {
    "site": P("string", "The site name"),
    "operation": P("string", "The operation name"),
    "inputs": P("object", "The inputs for this call, e.g. {\"query\": \"redis\"}"),
}, required=["site", "operation"], risk=_call_risk, group="site_api")
def site_api_call(site: str, operation: str, inputs: dict | None = None):
    spec = S.get_spec(site, operation)
    if not spec:
        return (f"NOT RUN: no operation '{operation}' for '{site}'. site_api_list shows what's learned; teach it with "
                "site_api_learn (read) or site_api_record (write).")
    out = R.call(site, operation, inputs or {}, allow_write=(spec.get("kind") == "write"))
    if out.get("ok"):
        data = out.get("data")
        note = " (re-learned: the site had changed)" if out.get("healed") else ""
        return {"ok": True, "via": f"site API, tier {out['tier']}{note}", "data": data}
    return {"ok": False, "class": out.get("class"), "reason": out.get("reason"),
            "did_it_happen": "unknown - check the site" if out.get("class") == "unconfirmed" else "no, nothing changed"}


@tool("site_api_list", "List the site operations Karya has learned (so you can call them with site_api_call instead of "
      "the browser).", {"site": P("string", "Optional: only this site")}, group="site_api")
def site_api_list(site: str = ""):
    sites = [S.load_site(site)] if site else S.all_sites()
    out = []
    for data in sites:
        if not data:
            continue
        for name, spec in (data.get("operations") or {}).items():
            out.append({"site": data["site"], "operation": name, "kind": spec.get("kind", "read"),
                        "params": spec.get("params", []), "domain": spec.get("domain")})
    return {"operations": out} if out else {"operations": [], "note": "Nothing learned yet. Use site_api_scout then "
            "site_api_learn on a site you use a lot, so applying or searching there becomes one fast call."}


@tool("site_api_forget", "Forget a learned site operation (or a whole site).", {
    "site": P("string", "The site name"),
    "operation": P("string", "Optional: just this operation; omit to forget the whole site"),
}, required=["site"], risk=CRITICAL, group="site_api",
      summary=lambda a: f"Forget site API {a.get('site')}" + (f"/{a['operation']}" if a.get("operation") else " (all operations)"))
def site_api_forget(site: str, operation: str = ""):
    ok = S.forget(site, operation or None)
    return {"forgot": ok, "site": site, "operation": operation or "(all)"} if ok else f"Nothing learned for {site}."
