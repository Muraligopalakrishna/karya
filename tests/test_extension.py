"""Karya Browser Link (own Chrome) and the honest-submit checks.

- page.js (the code that runs inside web pages) is tested in a real headless Chrome on a local form that behaves like
  Ashby/Greenhouse: question labels without <label for>, React-controlled inputs, a 'Start typing...' combobox,
  Yes/No buttons, a required file upload and a server-style error message after Submit.
- The extension backend is tested with a fake extension that runs the same page.js in that Chrome page.
- The /ext WebSocket endpoint is tested on a real local server (token, origin, hello)."""
import asyncio
import functools
import http.server
import json
import socket
import threading
import time

import pytest

from karya import ext_link
from karya.config import settings
from karya.registry import CRITICAL, TOOLS, run_tool
from karya.tools import browser

FORM = r"""<!doctype html><html><head><title>Associate Product Manager - Acme</title>
<style>.opts{display:none}.opts.open{display:block}.chosen{outline:2px solid green}</style></head><body>
<h1>Associate Product Manager at Acme</h1>
<form id="app" novalidate>
  <div class="field"><div class="question-title">Name *</div><input id="name" type="text" placeholder="Type here..."></div>
  <div class="field"><div class="question-title">Email *</div><input id="email" type="email" placeholder="hello@example.com"></div>
  <div class="field"><div class="question-title">Location</div>
    <input id="loc" role="combobox" aria-autocomplete="list" placeholder="Start typing...">
    <ul id="loc-opts" class="opts" role="listbox"></ul></div>
  <fieldset><legend>Do you have at least 2 years of B2B SaaS experience? *</legend>
    <button type="button" class="yn" data-v="Yes">Yes</button><button type="button" class="yn" data-v="No">No</button></fieldset>
  <div class="field"><div class="question-title">Notice period</div>
    <select id="notice"><option value="">Select...</option><option>Immediately</option><option>30 days</option></select></div>
  <div class="field"><div class="question-title">Resume *</div><input id="cv" type="file" style="display:none">
    <button type="button" id="upload-btn" onclick="document.getElementById('cv').click()">Upload File</button></div>
  <button type="submit" id="submit">Submit Application</button>
</form>
<p id="status">ready</p>
<script>
  // React-like: keeps its own state and only learns about typing through input events
  const state = {name: "", email: "", loc: "", yn: "", file: ""};
  for (const id of ["name", "email", "loc"]) {
    document.getElementById(id).addEventListener("input", (e) => {
      state[id] = e.target.value;
      if (id === "loc") {
        const ul = document.getElementById("loc-opts");
        ul.innerHTML = "";
        ["Hyderabad, Telangana, India", "Hyderabad, Sindh, Pakistan"].filter((c) => c.toLowerCase().startsWith(e.target.value.toLowerCase()))
          .forEach((c) => { const li = document.createElement("li"); li.setAttribute("role", "option"); li.textContent = c;
            li.addEventListener("click", () => { state.loc = c; document.getElementById("loc").value = c; ul.classList.remove("open"); });
            ul.appendChild(li); });
        ul.classList.toggle("open", ul.children.length > 0);
      }
    });
  }
  document.querySelectorAll(".yn").forEach((b) => b.addEventListener("click", () => {
    state.yn = b.dataset.v; document.querySelectorAll(".yn").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false")); }));
  document.getElementById("cv").addEventListener("change", (e) => { state.file = e.target.files[0] ? e.target.files[0].name : ""; });
  document.getElementById("app").addEventListener("submit", (e) => {
    e.preventDefault();
    const missing = [];
    if (!state.name) missing.push("Name"); if (!state.email) missing.push("Email"); if (!state.yn) missing.push("B2B SaaS experience");
    if (!state.file) missing.push("Resume");
    const s = document.getElementById("status");
    s.textContent = missing.length ? "Your form needs corrections. Missing entry for required field: " + missing.join(", ")
                                   : "Thank you for applying! We received your application. (" + JSON.stringify(state) + ")";
  });
  window.__state = state;
</script></body></html>"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- pure logic (no browser)
def test_submit_verdict():
    before = "Name *\nEmail *\nSubmit Application"
    v = browser.submit_verdict("https://jobs.ashbyhq.com/x/application", before, "https://jobs.ashbyhq.com/x/application",
                               before + "\nYour form needs corrections\nMissing entry for required field: Name")
    assert v.startswith("RESULT: NOT SUBMITTED") and "Missing entry" in v
    v = browser.submit_verdict("https://x.com/apply", before, "https://x.com/apply", "Thank you for applying! We'll be in touch.")
    assert v.startswith("RESULT: SUBMITTED")
    assert browser.submit_verdict("https://a.com/jobs/1/apply", before, "https://a.com/jobs/1/thank-you", "").startswith("RESULT: SUBMITTED")
    # nothing new on the page: never claim success; old error text that was already there doesn't count either
    assert browser.submit_verdict("https://a.com/apply", before, "https://a.com/apply", before).startswith("RESULT: UNCONFIRMED")
    assert browser.submit_verdict("u", "This field is required", "u", "This field is required").startswith("RESULT: UNCONFIRMED")


def test_field_action():
    assert browser.field_action({"tag": "select"}, "India") == ("select", "India")
    assert browser.field_action({"tag": "input", "type": "checkbox"}, "false") == ("check", False)
    assert browser.field_action({"tag": "input", "type": "file"}, "C:/cv.pdf") == ("upload", "C:/cv.pdf")
    assert browser.field_action({"tag": "button", "label": "No"}, "No") == ("click", None)
    with pytest.raises(ValueError):  # "Yes" sent to the "No" button: refuse instead of clicking the wrong answer
        browser.field_action({"tag": "button", "label": "No"}, "Yes")
    assert browser.field_action({"tag": "input", "type": "text"}, "Asha") == ("fill", "Asha")
    assert browser.field_action({"tag": "div", "editable": True}, "Hi") == ("type", "Hi")


def test_submit_like_clicks():
    assert browser.is_submit_click({"tag": "button"}, "Submit Application", "https://jobs.ashbyhq.com/x")
    assert browser.is_submit_click({"tag": "button"}, "Submit", "https://example.org/form")
    assert not browser.is_submit_click({"tag": "button"}, "Apply", "https://jobs.ashbyhq.com/x")  # only opens the form
    assert browser.is_submit_click({"tag": "button"}, "Post", "https://www.linkedin.com/feed/")   # posts are verified too
    assert browser.is_post_click({"tag": "button"}, "Post", "https://www.linkedin.com/feed/")
    assert not browser.is_post_click({"tag": "button"}, "Send", "https://www.linkedin.com/messaging/")   # a DM


def test_upload_precheck_blocks_resume_for_another_company(tmp_path, monkeypatch):
    from karya.tools import resume
    pdf = settings.workspace / "resumes" / "Asha_Resume_FlickTV.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4")
    resume.remember_tailored(pdf, "Flick TV", "Product Analyst")
    assert resume.tailored_for(pdf)["company"] == "Flick TV"

    class Fake(browser.BrowserSession):
        def call(self, fn, *a):
            return fn(*a)

        def all_text(self):
            return "Associate Product Manager at SpotDraft. Upload your resume."
    fake = Fake()
    fake.url, fake.title = "https://jobs.ashbyhq.com/spotdraft/123/application", "SpotDraft"
    monkeypatch.setattr(browser, "_current", lambda: fake)
    stop = browser._upload_precheck({"file_path": str(pdf), "element_id": 3})
    assert stop and stop.startswith("NOT UPLOADED") and "Flick TV" in stop
    assert browser._upload_precheck({"file_path": str(pdf), "element_id": 3, "any_resume": True}) is None
    fake.title = "Flick TV - Product Analyst"
    assert browser._upload_precheck({"file_path": str(pdf), "element_id": 3}) is None  # right company
    assert browser._upload_precheck({"file_path": str(tmp_path / "master.pdf"), "element_id": 3}) is None  # not tailored


def test_extension_id_is_stable_and_config_written(tmp_path):
    assert ext_link.extension_id() == "lhbnofhflkleekomggemljdnemealmcd"
    ext_link.write_extension_config(8765, "tok", folder=tmp_path)
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"port": 8765, "token": "tok"}


def test_routing_follows_browser_mode(monkeypatch):
    monkeypatch.setattr(settings, "browser_mode", "auto")
    monkeypatch.setattr(ext_link.link, "ws", None)
    monkeypatch.setattr(ext_link.link, "last_used", 0.0)
    assert browser._use() is browser.session                      # no extension: Karya's own window
    monkeypatch.setattr(type(ext_link.link), "connected", property(lambda self: True))
    assert browser._use() is browser.ext_session                  # extension connected: your Chrome
    monkeypatch.setattr(settings, "browser_mode", "karya")
    assert browser._use() is browser.session
    monkeypatch.setattr(type(ext_link.link), "connected", property(lambda self: False))
    monkeypatch.setattr(ext_link.link, "wait_connected", lambda s: False)
    monkeypatch.setattr(settings, "browser_mode", "chrome")
    with pytest.raises(RuntimeError, match="isn't connected"):      # 'only my Chrome' never falls back silently
        browser._use()
    monkeypatch.setattr(settings, "browser_mode", "auto")
    monkeypatch.setattr(ext_link.link, "last_used", time.time())  # was using your Chrome a minute ago
    with pytest.raises(RuntimeError, match="isn't connected"):
        browser._use()


# ---------------------------------------------------------------- page.js in a real (headless) Chrome
@pytest.fixture(scope="module")
def form_site(tmp_path_factory):
    root = tmp_path_factory.mktemp("form")
    (root / "apply.html").write_text(FORM, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/apply.html"
    server.shutdown()


@pytest.fixture(scope="module")
def page(form_site):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    chrome = None
    for channel in ("chrome", "msedge", None):
        try:
            chrome = pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch(headless=True)
            break
        except Exception:
            continue
    if chrome is None:
        pw.stop()
        pytest.skip("no Chrome/Edge available")
    pg = chrome.new_page()
    yield pg
    chrome.close()
    pw.stop()


class FakeExtension:
    """Answers link.request the way background.js does, by running page.js in a real page (one frame)."""

    def __init__(self, pg, url):
        self.page, self.start_url, self.calls = pg, url, []

    def request(self, method, params=None, timeout=45.0):
        p = params or {}
        self.calls.append(method)
        pg = self.page
        if method == "open":
            pg.goto(p["url"])
            return {"url": pg.url, "title": pg.title()}
        if method == "settle":
            pg.wait_for_timeout(min(int(p.get("ms") or 0), 300))
            return {"url": pg.url, "title": pg.title()}
        if method == "snapshot":
            res = pg.evaluate(browser.PAGE_CALL_JS, {"fn": "snapshot", "args": {
                "next": p.get("next", 1), "reset": p.get("reset", False), "max": p.get("max", 400)}})
            for it in res["items"]:
                it["frame"] = 0
            return {"url": pg.url, "title": pg.title(), "items": res["items"], "next": res["next"],
                    "text": pg.evaluate("() => document.body.innerText")[:p.get("text_chars", 2000)]}
        if method == "act":
            return pg.evaluate("async (a) => {\n" + browser.PAGE_JS + "\nreturn await window.__karya.act(a.op, a.args);\n}",
                               {"op": p["op"], "args": p.get("args") or {}})
        if method == "text":
            return {"url": pg.url, "title": pg.title(), "text": pg.evaluate("() => document.body.innerText")}
        raise AssertionError(f"unexpected {method}")


@pytest.fixture()
def chrome_link(page, form_site, monkeypatch):
    fake = FakeExtension(page, form_site)
    monkeypatch.setattr(settings, "browser_mode", "chrome")
    monkeypatch.setattr(type(ext_link.link), "connected", property(lambda self: True))
    monkeypatch.setattr(ext_link.link, "request", fake.request)
    browser.ext_session.items, browser.ext_session.url = {}, ""
    return fake


def _find(label_part, kind=None):
    for it in browser.ext_session.items.values():
        if label_part.lower() in (it.get("label") or "").lower() and (kind is None or it.get("tag") == kind):
            return it
    raise AssertionError(f"{label_part} not found: {[i.get('label') for i in browser.ext_session.items.values()]}")


def test_snapshot_uses_questions_not_placeholders(chrome_link, form_site):
    snap = run_tool("browser_open", {"url": form_site})
    assert "(Karya's tab in your Chrome)" in snap
    name = _find("Name *")
    assert name["required"] and name.get("placeholder") == "Type here..."
    assert _find("Location")["role"] == "combobox"
    yes = _find("Yes", "button")
    assert "B2B SaaS" in yes.get("question", "")                 # Yes/No buttons know their question
    assert _find("Resume", "input")["type"] == "file"
    assert '"Name *"' in snap and "*required" in snap and "(question: Do you have at least 2 years" in snap


def test_empty_required_fields_block_submit(chrome_link, form_site):
    run_tool("browser_open", {"url": form_site})
    submit = _find("Submit Application")
    args = {"element_id": submit["id"]}
    assert TOOLS["browser_click"].assess(args)[0] == CRITICAL
    stop = TOOLS["browser_click"].precheck(args)
    assert stop.startswith("NOT CLICKED") and "Name" in stop and "Email" in stop and "Resume" in stop
    assert "B2B SaaS" in stop and "Location" not in stop and "Notice" not in stop   # optional fields aren't listed
    assert TOOLS["browser_click"].precheck({**args, "confirm_empty": True}) is None


def test_fill_combobox_buttons_upload_and_verified_submit(chrome_link, form_site, tmp_path):
    from karya import answers
    run_tool("browser_open", {"url": form_site})
    # questions only the user can answer are held back until they answer them
    held = run_tool("browser_fill", {"fields": {str(_find("Notice period")["id"]): "Immediately",
                                                str(_find("No", "button")["id"]): "No"}})
    assert held.startswith("NOT FILLED") and "Notice period" in held and "B2B SaaS" in held and "ask_user" in held
    assert chrome_link.page.evaluate("() => window.__state.yn") == ""      # nothing was clicked
    answers.save("Notice period", "30 days")                              # what the user typed in the card
    answers.save("Do you have at least 2 years of B2B SaaS experience?", "No")
    answers.save("Location", "Hyderabad, India")
    cv = tmp_path / "Asha_Resume_Acme.pdf"
    cv.write_bytes(b"%PDF-1.4 test resume")
    fields = {str(_find("Name *")["id"]): "Asha Rao", str(_find("Email *")["id"]): "asha@example.org",
              str(_find("Location")["id"]): "Hyderabad", str(_find("No", "button")["id"]): "No",
              str(_find("Notice period")["id"]): "30 days", str(_find("Resume", "input")["id"]): str(cv)}
    out = run_tool("browser_fill", {"fields": fields})
    assert "Filled 6 field(s)" in out and "Problems" not in out, out
    assert 'picked "Hyderabad, Telangana, India"' in out
    state = chrome_link.page.evaluate("() => window.__state")
    assert state == {"name": "Asha Rao", "email": "asha@example.org", "loc": "Hyderabad, Telangana, India",
                     "yn": "No", "file": "Asha_Resume_Acme.pdf"}       # the page's own state saw every change
    # a wrong answer for a Yes/No button is refused, not clicked
    bad = run_tool("browser_fill", {"fields": {str(_find("No", "button")["id"]): "Yes"}})
    assert "Problems" in bad and "use the id of the \"Yes\" button" in bad
    submit = _find("Submit Application")
    assert TOOLS["browser_click"].precheck({"element_id": submit["id"]}) is None   # nothing empty now
    result = run_tool("browser_click", {"element_id": submit["id"]})
    assert "RESULT: SUBMITTED" in result and "Thank you for applying" in result


def test_failed_submit_is_reported_as_not_submitted(chrome_link, form_site):
    run_tool("browser_open", {"url": form_site})
    run_tool("browser_fill", {"fields": {str(_find("Name *")["id"]): "Asha Rao"}})
    result = run_tool("browser_click", {"element_id": _find("Submit Application")["id"]})
    assert "RESULT: NOT SUBMITTED" in result and "Missing entry for required field" in result


def test_ids_stay_the_same_across_snapshots(chrome_link, form_site):
    run_tool("browser_open", {"url": form_site})
    first = {it["label"]: it["id"] for it in browser.ext_session.items.values()}
    chrome_link.page.evaluate("() => { const b = document.createElement('button'); b.textContent = 'New button'; "
                              "document.body.prepend(b); window.scrollTo(0, document.body.scrollHeight); }")
    run_tool("browser_snapshot", {})
    second = {it["label"]: it["id"] for it in browser.ext_session.items.values()}
    for label in ("Name *", "Email *", "Notice period", "Submit Application"):
        assert first[label] == second[label]
    assert second["New button"] > max(first.values())                        # new elements get new numbers


def test_actions_planned_from_one_snapshot_hit_the_right_fields(chrome_link, form_site):
    """The Zeta run: fill, upload and select sent together; the fill re-scanned the page in between."""
    from karya import answers
    answers.save("Notice period", "30 days")
    run_tool("browser_open", {"url": form_site})
    name_id, notice_id = _find("Name *")["id"], _find("Notice period")["id"]
    chrome_link.page.evaluate("() => { const b = document.createElement('a'); b.href = '#'; b.textContent = 'Banner'; "
                              "document.body.prepend(b); }")                   # would have shifted every number
    run_tool("browser_fill", {"fields": {str(name_id): "Asha Rao"}})
    out = run_tool("browser_select", {"element_id": notice_id, "option": "30 days"})
    assert 'Selected "30 days"' in out
    assert chrome_link.page.evaluate("() => document.getElementById('notice').value") == "30 days"


def test_dropdown_wrappers_and_custom_lists(page, form_site):
    page.goto(form_site)
    page.evaluate("""() => {
      document.getElementById('notice').closest('.field').setAttribute('data-jid', '900');
      const li = document.createElement('li'); li.setAttribute('data-jid', '901');
      li.innerHTML = '<div>Gender</div><div role="button" data-jid="902" id="dd">Select...</div>';
      document.body.appendChild(li);
      document.getElementById('dd').addEventListener('click', () => {
        const ul = document.createElement('ul'); ul.setAttribute('role', 'listbox');
        ul.innerHTML = '<li role="option">Male</li><li role="option">Female</li>';
        ul.querySelectorAll('li').forEach((o) => o.addEventListener('click', () => { document.getElementById('dd').textContent = o.textContent; }));
        document.body.appendChild(ul);
      });
    }""")
    act = "async (a) => {\n" + browser.PAGE_JS + "\nreturn await window.__karya.act(a.op, a.args);\n}"
    assert page.evaluate(act, {"op": "select", "args": {"id": 900, "option": "30 days"}})["picked"] == "30 days"
    assert page.evaluate("() => document.getElementById('notice').value") == "30 days"   # the select inside the wrapper
    res = page.evaluate(act, {"op": "select", "args": {"id": 902, "option": "Female"}})
    assert res["picked"] == "Female" and page.evaluate("() => document.getElementById('dd').textContent") == "Female"


def test_suggestions_match_whole_words(page, form_site):
    page.goto(form_site)
    match = "(a) => {\n" + browser.PAGE_JS + "\nconst opts = a.opts.map((t) => { const li = document.createElement('li'); " \
            "li.setAttribute('role', 'option'); li.textContent = t; document.body.appendChild(li); return li; });\n" \
            "const best = window.__karya.markOption({value: a.value});\nopts.forEach((o) => o.remove());\nreturn best;\n}"
    cities = ["Indianapolis, Indiana, United States", "Indiana, United States", "Indore, Madhya Pradesh, India"]
    found = page.evaluate(match, {"opts": cities, "value": "India"})
    assert "text" not in found and found["options"][0].startswith("Indianapolis")   # no guess: report the list
    assert page.evaluate(match, {"opts": cities + ["India"], "value": "India"})["text"] == "India"
    two = ["Hyderabad, Sindh, Pakistan", "Hyderabad, Telangana, India"]
    assert page.evaluate(match, {"opts": two, "value": "Hyderabad, India"})["text"] == "Hyderabad, Telangana, India"
    assert page.evaluate(match, {"opts": ["Bengaluru, Karnataka, India"], "value": "Bengal"}).get("text") is None


def test_lever_style_location_field_picks_a_suggestion(page, form_site):
    page.goto(form_site)
    page.evaluate("""() => {
      const wrap = document.createElement('div');
      wrap.innerHTML = '<label for="lv">Current location ✱</label><input id="lv" class="location-input" name="location">' +
                       '<div class="dropdown-results"></div><input type="hidden" id="chosen">';
      document.body.appendChild(wrap);
      document.getElementById('lv').addEventListener('input', (e) => {
        const box = wrap.querySelector('.dropdown-results'); box.innerHTML = '';
        ['Hyderabad, Telangana, India', 'Hyderabad, Sindh, Pakistan'].filter((c) => c.toLowerCase().startsWith(e.target.value.toLowerCase().split(',')[0].trim()))
          .forEach((c) => { const d = document.createElement('div'); d.className = 'dropdown-location'; d.textContent = c;
            d.addEventListener('click', () => { document.getElementById('chosen').value = c; e.target.value = c; box.innerHTML = ''; });
            box.appendChild(d); });
      });
      document.getElementById('lv').setAttribute('data-jid', '950');
    }""")
    act = "async (a) => {\n" + browser.PAGE_JS + "\nreturn await window.__karya.act(a.op, a.args);\n}"
    res = page.evaluate(act, {"op": "set", "args": {"id": 950, "value": "Hyderabad, India"}})
    assert res["picked"] == "Hyderabad, Telangana, India"
    assert page.evaluate("() => document.getElementById('chosen').value") == "Hyderabad, Telangana, India"
    res = page.evaluate(act, {"op": "set", "args": {"id": 950, "value": "Hyd"}})   # partial: no guess, show the list
    assert res["picked"] is None and "Hyderabad, Telangana, India" in res["options"]


LINKEDIN_RADIOS = """<!doctype html><html><head><title>Easy Apply</title><style>
.vh { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.opt input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.opt label { display: inline-block; padding: 6px 12px; border: 1px solid #888; border-radius: 12px; }
</style></head><body><form id="ea">
<fieldset><legend><span aria-hidden="true">Will you relocate to Bangalore?</span><span class="vh">Will you relocate to Bangalore?</span>
  <span class="required-mark">Required</span></legend>
  <div class="opt"><input type="radio" id="r-yes" name="reloc" value="Yes" required><label for="r-yes">Yes</label></div>
  <div class="opt"><input type="radio" id="r-no" name="reloc" value="No"><label for="r-no">No</label></div>
</fieldset>
<button type="submit">Submit application</button></form>
<script>document.getElementById('ea').addEventListener('change', (e) => { window.__picked = e.target.value; });</script>
</body></html>"""


def test_linkedin_style_hidden_radio_choices(page, chrome_link, form_site, monkeypatch):
    """'Will you relocate to Bangalore?' Yes/No where the real inputs are hidden behind their labels."""
    from karya import answers
    page.set_content(LINKEDIN_RADIOS)
    monkeypatch.setattr(chrome_link, "request", _same_page_request(chrome_link))
    snap = run_tool("browser_snapshot", {})
    yes = next(it for it in browser.ext_session.items.values() if it.get("label") == "Yes")
    no = next(it for it in browser.ext_session.items.values() if it.get("label") == "No")
    assert yes["type"] == "radio" and yes.get("question", "").startswith("Will you relocate to Bangalore?")
    assert "(question: Will you relocate to Bangalore? *)" in snap and "[ ]" in snap         # listed, readable, unticked
    submit = next(it for it in browser.ext_session.items.values() if it.get("label") == "Submit application")
    assert "Will you relocate" in TOOLS["browser_click"].precheck({"element_id": submit["id"]})   # required, still empty
    blocked = TOOLS["browser_check"].precheck({"element_id": yes["id"], "checked": True})
    assert blocked.startswith("NOT TICKED") and "relocate" in blocked                      # the user's own answer
    assert TOOLS["browser_click"].precheck({"element_id": yes["id"]}).startswith("NOT CLICKED")
    answers.save("Will you relocate to Bangalore?", "Yes")
    assert TOOLS["browser_check"].precheck({"element_id": no["id"], "checked": True}) is None
    run_tool("browser_check", {"element_id": no["id"], "checked": True})
    assert page.evaluate("() => window.__picked") == "No" and page.evaluate("() => document.getElementById('r-no').checked")
    out = run_tool("browser_click", {"element_id": yes["id"]})
    assert page.evaluate("() => document.getElementById('r-yes').checked") and "Clicked" in out
    assert TOOLS["browser_click"].precheck({"element_id": submit["id"]}) is None


def test_canvas_and_chess_in_your_chrome(page, chrome_link, form_site, monkeypatch):
    """Through the extension (simulated pointer events in the page): canvas clicks/drags and chess moves."""
    import re as _re
    from .test_mcp import BOARD
    page.set_content(BOARD)
    monkeypatch.setattr(chrome_link, "request", _same_page_request(chrome_link))
    snap = run_tool("browser_snapshot", {})
    assert "CHESS BOARD (chess.com; White is at the bottom)" in snap and "White: Ke1 Pe2" in snap
    moved = run_tool("browser_move_piece", {"from_square": "e2", "to_square": "e4"})
    assert moved.startswith("Moved e2-e4") and "Pe4" in moved, moved
    assert run_tool("browser_move_piece", {"from_square": "e7", "to_square": "e5"}).startswith("NOT MOVED")
    run_tool("browser_snapshot", {})
    canvas = next(it for it in browser.ext_session.items.values() if it.get("area") and it["tag"] == "canvas")
    run_tool("browser_click_at", {"x": 0.5, "y": 0.5, "element_id": canvas["id"]})
    x, y = map(int, _re.search(r"clicked (\d+),(\d+)", page.evaluate("() => document.getElementById('log').textContent")).groups())
    assert abs(x - 151) <= 1 and abs(y - 101) <= 1
    run_tool("browser_drag", {"x": 0.1, "y": 0.5, "to_x": 0.6, "to_y": 0.5, "element_id": canvas["id"]})
    assert "dragged" in page.evaluate("() => document.getElementById('draglog').textContent")


def test_popup_over_the_board_is_reported(page, chrome_link, form_site, monkeypatch):
    from .test_mcp import BOARD
    page.set_content(BOARD)
    monkeypatch.setattr(chrome_link, "request", _same_page_request(chrome_link))
    page.evaluate("""() => { const m = document.createElement('div'); m.id = 'modal'; m.className = 'cc-modal';
      m.textContent = 'Game Review & Analysis'; m.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5)';
      document.body.appendChild(m); }""")
    run_tool("browser_snapshot", {})
    out = run_tool("browser_move_piece", {"from_square": "e2", "to_square": "e4"})
    assert out.startswith("NOT MOVED") and "covering the board" in out and "Game Review" in out
    assert "square-52" in page.evaluate("() => document.querySelector('.wp').className")       # nothing moved
    page.evaluate("() => document.getElementById('modal').remove()")
    assert run_tool("browser_move_piece", {"from_square": "e2", "to_square": "e4"}).startswith("Moved e2-e4")


def _same_page_request(fake):
    """The fake extension, but without navigating (the page content was set directly)."""
    original = fake.request

    def request(method, params=None, timeout=45.0):
        if method == "settle":
            return {"url": fake.page.url, "title": fake.page.title()}
        return original(method, params, timeout)
    return request


def test_submit_card_lists_what_will_be_sent(chrome_link, form_site, tmp_path):
    from karya import answers
    answers.save("Notice period", "30 days")
    run_tool("browser_open", {"url": form_site})
    run_tool("browser_fill", {"fields": {str(_find("Name *")["id"]): "Asha Rao", str(_find("Notice period")["id"]): "30 days"}})
    level, card = TOOLS["browser_click"].assess({"element_id": _find("Submit Application")["id"]})
    assert level == CRITICAL and "What this form will send" in card
    assert "- Name: Asha Rao" in card and "- Notice period: 30 days" in card


def test_playwright_window_uses_the_same_page_code(page, form_site, tmp_path, monkeypatch):
    """Karya's own window shares page.js (questions, required flags, suggestion picking)."""
    session = browser.BrowserSession()
    old = (settings.browser_headless, settings.browser_mode)
    settings.browser_headless, settings.browser_mode = True, "karya"
    session.profile_dir = tmp_path / "profile"
    monkeypatch.setattr(browser, "session", session)
    try:
        snap = run_tool("browser_open", {"url": form_site})
        assert '"Name *"' in snap and "(question: Do you have at least 2 years" in snap
        loc = next(it for it in session.items.values() if it.get("label") == "Location")
        from karya import answers
        answers.save("Location", "Hyderabad, India")
        out = run_tool("browser_type", {"element_id": loc["id"], "text": "Hyderabad"})
        assert 'picked "Hyderabad, Telangana, India"' in out
        submit = next(it for it in session.items.values() if it.get("label") == "Submit Application")
        assert TOOLS["browser_click"].precheck({"element_id": submit["id"]}).startswith("NOT CLICKED")
    finally:
        session.shutdown()
        settings.browser_headless, settings.browser_mode = old


# ---------------------------------------------------------------- /ext endpoint on a real server
@pytest.fixture()
def live_server(monkeypatch):
    import uvicorn
    from karya.agent import Agent
    from karya.server import create_app

    from .conftest import FakeLLM
    port = _free_port()
    app = create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="secret-token-123456789012", port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield port
    server.should_exit = True
    thread.join(5)


def test_ext_endpoint_accepts_only_the_extension(live_server):
    import websockets
    port = live_server
    good_origin = f"chrome-extension://{ext_link.extension_id()}"

    async def scenario():
        url = f"ws://127.0.0.1:{port}/ext?token=secret-token-123456789012"
        for bad_url, origin in ((url.replace("123456789012", "wrong"), good_origin),       # wrong token
                                (url, "chrome-extension://abcdefghijklmnopabcdefghijklmnop"),  # another extension
                                (url, "https://evil.example")):                          # a website
            with pytest.raises(Exception):
                async with websockets.connect(bad_url, origin=origin) as ws:
                    await ws.send(json.dumps({"type": "hello"}))
                    await asyncio.wait_for(ws.recv(), 2)
        async with websockets.connect(url, origin=good_origin) as ws:
            await ws.send(json.dumps({"type": "hello", "version": "1.0.0", "ua": "Mozilla/5.0 Chrome/154.0"}))
            for _ in range(40):
                if ext_link.link.connected:
                    break
                await asyncio.sleep(0.05)
            assert ext_link.link.connected and ext_link.link.status()["browser"] == "Chrome"
            assert ext_link.link.status()["needs_reload"] is True                     # Setup says an update is ready
            # a tool thread asks the extension something; the fake extension answers
            answer = asyncio.get_running_loop().run_in_executor(None, ext_link.link.request, "ping", {}, 5)
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert msg["method"] == "ping"
            await ws.send(json.dumps({"id": msg["id"], "result": {"pong": True}}))
            assert (await answer) == {"pong": True}
        for _ in range(40):
            if not ext_link.link.connected:
                break
            await asyncio.sleep(0.05)
        assert not ext_link.link.connected

    errors = []

    def runner():
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            errors.append(exc)
    thread = threading.Thread(target=runner)
    thread.start()
    thread.join(30)
    if errors:
        raise errors[0]
