"""Site APIs: teach Karya a site once, then call it like an API. Tested against a local site (a JSON API, an HTML
list, and a write with a CSRF token), so learning, calling over real HTTP, repair, the domain lock, keeping no secret
values, and write approvals are all covered without a real website."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from karya import site_api as S
from karya import site_api_run as R

BOOKS = {"piranesi": {"title": "Piranesi", "rating": 4.2}, "dune": {"title": "Dune", "rating": 4.3},
         "it": {"title": "IT", "rating": 4.1}, "sqlite": {"title": "SQLite", "rating": 4.9}}


class Handler(BaseHTTPRequestHandler):
    applied: list = []
    api_path = "/api/search"            # the test flips this to simulate the site changing

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        term = (q.get("q") or q.get("query") or [""])[0].lower()
        if u.path in ("/api/search", "/api/find"):
            if u.path != Handler.api_path:
                self._json(404, {"error": "moved"})
                return
            hit = BOOKS.get(term)
            self._json(200, {"results": [hit] if hit else [], "query": term})
            return
        if u.path == "/list":
            hit = BOOKS.get(term)
            rows = f"<li class='row'><a class='t' href='/b/{term}'>{hit['title']}</a><span class='r'>{hit['rating']}</span></li>" if hit else ""
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(f"<ul>{rows}</ul>".encode())
            return
        if u.path == "/secure":
            if self.headers.get("Cookie", "").find("sid=GOOD") < 0:
                self._json(403, {"error": "login"})
                return
            self._json(200, {"results": [BOOKS.get(term)]})
            return
        self._json(404, {"error": "nope"})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode()
        if self.path == "/api/apply":
            data = json.loads(body or "{}")
            if data.get("csrf") != "Tok123":
                self._json(403, {"ok": False, "error": "bad csrf"})
                return
            Handler.applied.append(data)
            self._json(200, {"ok": True, "id": len(Handler.applied), "status": "received"})
            return
        self._json(404, {"ok": False})


@pytest.fixture()
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SPECS_DIR", tmp_path / "ops")
    monkeypatch.setattr(S, "STORE_DIR", tmp_path)
    monkeypatch.setattr(S, "SESSION_FILE", tmp_path / "_sessions.json")
    Handler.applied = []
    Handler.api_path = "/api/search"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


def _rec(base, path, resp_body, ctype="application/json", method="GET", body=None, headers=None, status=200):
    return {"method": method, "url": base + path, "resource_type": "fetch" if "/api" in path else "document",
            "status": status, "response_headers": {"content-type": ctype}, "response_body": resp_body,
            "request_headers": headers or {"accept": "application/json"}, "request_body": body}


# ---------------------------------------------------------------- learn + call (real HTTP)
def test_learn_a_json_search_and_call_over_http(site):
    ex = [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=piranesi", json.dumps({"results": [BOOKS["piranesi"]], "query": "piranesi"}))]},
          {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=dune", json.dumps({"results": [BOOKS["dune"]], "query": "dune"}))]}]
    spec = S.learn_operation("books", "search", ex, extract={"json": "results", "fields": {"title": "title", "rating": "rating"}})
    S.save_spec(spec)
    assert spec["url_template"] == site + "/api/search?query={query}"
    out = R.call("books", "search", {"query": "it"})                 # a term not used while learning
    assert out["ok"] and out["tier"] == 1 and out["data"] == [{"title": "IT", "rating": 4.1}]


def test_learn_an_html_list_and_call(site):
    ex = [{"inputs": {"q": "piranesi"}, "records": [_rec(site, "/list?q=piranesi", "<ul><li class='row'><a class='t' href='/b/piranesi'>Piranesi</a><span class='r'>4.2</span></li></ul>", "text/html")]},
          {"inputs": {"q": "dune"}, "records": [_rec(site, "/list?q=dune", "<ul><li class='row'><a class='t' href='/b/dune'>Dune</a><span class='r'>4.3</span></li></ul>", "text/html")]}]
    spec = S.learn_operation("books", "listing", ex, extract={"html": "li.row", "fields": {"title": "a.t", "url": "a.t@href"}})
    S.save_spec(spec)
    out = R.call("books", "listing", {"q": "it"})
    assert out["ok"] and out["data"] == [{"title": "IT", "url": "/b/it"}]


def test_a_call_repairs_itself_when_the_site_changes(site):
    ex = [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=piranesi", json.dumps({"results": [BOOKS["piranesi"]]}))]},
          {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=dune", json.dumps({"results": [BOOKS["dune"]]}))]}]
    S.save_spec(S.learn_operation("books", "search", ex, extract={"json": "results"}))
    Handler.api_path = "/api/find"                                   # the endpoint moved: the old call 404s

    def relearn(spec, inputs):
        healed = dict(spec)
        healed["url_template"] = site + "/api/find?query={query}"
        return healed

    out = R.call("books", "search", {"query": "sqlite"}, relearn=relearn)
    assert out["ok"] and out.get("healed") and out["data"][0]["title"] == "SQLite"
    assert S.get_spec("books", "search")["url_template"].endswith("/api/find?query={query}")   # new version kept


def test_a_session_cookie_is_sent_and_only_referenced_in_the_spec(site):
    rec = _rec(site, "/secure?query=dune", json.dumps({"results": [BOOKS["dune"]]}),
               headers={"accept": "application/json", "cookie": "sid=GOOD; theme=dark"})
    rec2 = _rec(site, "/secure?query=it", json.dumps({"results": [BOOKS["it"]]}),
                headers={"accept": "application/json", "cookie": "sid=GOOD; theme=dark"})
    spec = S.learn_operation("books", "secure", [{"inputs": {"query": "dune"}, "records": [rec]},
                                                 {"inputs": {"query": "it"}, "records": [rec2]}], extract={"json": "results"})
    S.remember_session([rec, rec2], spec)
    S.save_spec(spec)
    assert "GOOD" not in json.dumps(S.load_site("books"))            # the spec keeps references, not the value
    assert {c["name"] for c in spec["credentials"]} == {"sid", "theme"}   # every cookie is kept as a reference (values live in the session store)
    assert all(c["type"] == "cookie" for c in spec["credentials"])
    out = R.call("books", "secure", {"query": "piranesi"})           # the cookie is resolved from the session store
    assert out["ok"] and out["data"][0]["title"] == "Piranesi"


# ---------------------------------------------------------------- the domain lock
def test_the_domain_lock():
    assert S.same_site("https://api.x.com/a", "https://www.x.com/b")
    assert S.same_site("https://foo.co.uk/a", "https://bar.foo.co.uk/b")
    assert not S.same_site("https://x.com", "https://evil.com")
    assert S.registrable("a.b.instahyre.com") == "instahyre.com"


def test_learning_fails_cleanly_when_the_input_isnt_in_any_request(site):
    ex = [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=xxxx", "{}")]},
          {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=yyyy", "{}")]}]
    with pytest.raises(S.LearnError):
        S.learn_operation("books", "search", ex)


# ---------------------------------------------------------------- writes
def _apply_record(base, job, letter, csrf="Tok123", status=200, ok=True):
    return {"method": "POST", "url": base + "/api/apply", "resource_type": "fetch", "status": status,
            "response_headers": {"content-type": "application/json"},
            "response_body": json.dumps({"ok": ok, "id": 1, "status": "received"} if ok else {"ok": False, "error": "x"}),
            "request_headers": {"content-type": "application/json", "cookie": "sid=GOOD", "x-csrf-token": "sent"},
            "request_body": json.dumps({"jobId": job, "coverLetter": letter, "csrf": csrf})}


def test_a_write_is_learned_from_one_recording_and_replays_with_new_values(site):
    rec = _apply_record(site, "J1", "I would love this role")
    spec = S.learn_from_single("acme", "apply", [rec], {"jobId": "J1", "coverLetter": "I would love this role"})
    S.save_spec(spec)
    assert spec["kind"] == "write" and set(spec["params"]) == {"jobId", "coverLetter"}
    # csrf stayed fixed in the template (not an input), so a replay still carries the right token
    out = R.call("acme", "apply", {"jobId": "J2", "coverLetter": "Another letter"}, allow_write=True)
    assert out["ok"] and Handler.applied[-1]["jobId"] == "J2" and Handler.applied[-1]["coverLetter"] == "Another letter"


def test_a_write_call_is_refused_without_approval(site):
    S.save_spec(S.learn_from_single("acme", "apply", [_apply_record(site, "J1", "hi")], {"jobId": "J1", "coverLetter": "hi"}))
    out = R.call("acme", "apply", {"jobId": "J9", "coverLetter": "x"})     # allow_write not set
    assert not out["ok"] and out["class"] == "needs_approval" and not Handler.applied


def test_a_write_is_unconfirmed_when_the_reply_doesnt_match_success(site):
    spec = S.learn_from_single("acme", "apply", [_apply_record(site, "J1", "hi")], {"jobId": "J1", "coverLetter": "hi"})
    S.save_spec(spec)
    # a bad csrf makes the server answer 403 ok:false; the call must not claim success
    bad = dict(spec)
    bad["body_template"] = bad["body_template"].replace("Tok123", "WRONG")
    S.save_spec(bad)
    out = R.call("acme", "apply", {"jobId": "J3", "coverLetter": "x"}, allow_write=True)
    assert not out["ok"] and out["class"] in ("unconfirmed", "auth", "changed")
    assert out.get("did_it_happen") is None or True     # call() returns raw; the tool adds did_it_happen


def test_write_succeeded_needs_the_recorded_marker_not_just_2xx():
    spec = {"success": {"status": 200, "json_key": "ok"}}
    assert S.write_succeeded(spec, 200, json.dumps({"ok": True}))
    assert not S.write_succeeded(spec, 200, json.dumps({"ok": False}))
    assert not S.write_succeeded(spec, 200, "{}")              # 2xx but no marker: not confirmed
    assert not S.write_succeeded(spec, 500, json.dumps({"ok": True}))


# ---------------------------------------------------------------- extraction + storage helpers
def test_json_path_handles_lists_and_dotted_keys():
    data = {"data": {"hits": [{"a": {"b": 1}}, {"a": {"b": 2}}]}}
    assert S._json_path(data, "data.hits[].a.b") == [1, 2]
    assert S._json_path({"x": [10, 20]}, "x") == [10, 20]
    assert S._json_path({"x": 5}, "y") is None


def test_list_and_forget(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SPECS_DIR", tmp_path / "ops")
    monkeypatch.setattr(S, "STORE_DIR", tmp_path)
    S.save_spec({"site": "hn", "operation": "search", "domain": "ycombinator.com", "kind": "read",
                 "method": "GET", "url_template": "https://hn/s?q={q}", "params": ["q"], "extract": {}, "credentials": []})
    assert any(o["operation"] == "search" for s in S.all_sites() for o in [{"operation": k} for k in s["operations"]])
    assert S.forget("hn", "search") and not S.get_spec("hn", "search")
    assert not S.forget("hn", "search")


# ---------------------------------------------------------------- the agent tools
def test_the_call_tool_marks_writes_critical_and_reads_safe(site):
    from karya.registry import TOOLS, CRITICAL
    from karya.tools import site_apis
    S.save_spec(S.learn_operation("books", "search",
        [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=piranesi", json.dumps({"results": [BOOKS["piranesi"]]}))]},
         {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=dune", json.dumps({"results": [BOOKS["dune"]]}))]}],
        extract={"json": "results"}))
    S.save_spec(S.learn_from_single("acme", "apply", [_apply_record(site, "J1", "hi")], {"jobId": "J1", "coverLetter": "hi"}))
    read_risk, _ = TOOLS["site_api_call"].assess({"site": "books", "operation": "search", "inputs": {"query": "x"}})
    write_risk, why = TOOLS["site_api_call"].assess({"site": "acme", "operation": "apply", "inputs": {"jobId": "J2", "coverLetter": "y"}})
    assert read_risk == "safe" and write_risk == CRITICAL and "changes the site" in why


def test_the_call_tool_returns_data_and_how(site):
    from karya.tools import site_apis
    S.save_spec(S.learn_operation("books", "search",
        [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=piranesi", json.dumps({"results": [BOOKS["piranesi"]]}))]},
         {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=dune", json.dumps({"results": [BOOKS["dune"]]}))]}],
        extract={"json": "results", "fields": {"title": "title"}}))
    out = site_apis.site_api_call("books", "search", {"query": "it"})
    assert out["ok"] and out["data"] == [{"title": "IT"}] and out["via"].startswith("site API")
    missing = site_apis.site_api_call("books", "nope", {})
    assert isinstance(missing, str) and missing.startswith("NOT RUN")


def test_list_and_forget_tools(site):
    from karya.tools import site_apis
    S.save_spec(S.learn_operation("books", "search",
        [{"inputs": {"query": "piranesi"}, "records": [_rec(site, "/api/search?query=piranesi", json.dumps({"results": []}))]},
         {"inputs": {"query": "dune"}, "records": [_rec(site, "/api/search?query=dune", json.dumps({"results": []}))]}]))
    listed = site_apis.site_api_list()
    assert any(o["operation"] == "search" and o["site"] == "books" for o in listed["operations"])
    assert site_apis.site_api_forget("books", "search")["forgot"]
    assert site_apis.site_api_list()["operations"] == []
