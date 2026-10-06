import functools
import http.server
import socket
import threading

import pytest

from karya.config import settings
from karya.registry import CRITICAL, SAFE, TOOLS, run_tool
from karya.tools import browser

pytestmark = pytest.mark.browser

PAGE = """<!doctype html><html><head><title>Apply - Test Co</title></head><body>
<h1>Frontend Developer at Test Co</h1>
<form id="search" onsubmit="event.preventDefault(); document.getElementById('status').textContent='searched ' + document.getElementById('q').value;">
  <input id="q" name="q" type="search" placeholder="Search jobs"><button type="submit">Search</button>
</form>
<form id="apply" onsubmit="event.preventDefault(); document.getElementById('status').textContent='SUBMITTED';">
  <label for="name">Full name</label><input id="name" required>
  <label for="country">Country</label><select id="country"><option>USA</option><option>India</option></select>
  <label><input type="checkbox" id="agree"> I agree</label>
  <label for="cv">Resume</label><input type="file" id="cv" onchange="document.getElementById('status').textContent='file ' + this.files[0].name">
  <div contenteditable="true" aria-label="Cover letter" id="editor"></div>
  <button type="button" onclick="document.getElementById('status').textContent='Step 2 of 3'">Next</button>
  <button type="submit">Submit application</button>
</form>
<a href="/page2.html">More jobs</a>
<p id="status">ready</p>
</body></html>"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    (root / "page2.html").write_text("<html><title>Page 2</title><body><p>Second page</p></body></html>", encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    old = (settings.browser_headless, browser.session.profile_dir)
    settings.browser_headless = True
    browser.session.profile_dir = tmp_path_factory.mktemp("profile")
    yield f"http://127.0.0.1:{server.server_port}/"
    browser.session.shutdown()
    settings.browser_headless, browser.session.profile_dir = old
    server.shutdown()


def _id(label):
    for it in browser.session.items.values():
        if it.get("label") == label:
            return it["id"]
    raise AssertionError(f"{label} not in snapshot: {[i.get('label') for i in browser.session.items.values()]}")


def test_full_form_flow(site, tmp_path):
    snap = run_tool("browser_open", {"url": site})
    assert "Title: Apply - Test Co" in snap and '"Full name"' in snap and '"Submit application"' in snap

    # search box + Enter is safe and works
    search_id = _id("Search jobs")
    assert TOOLS["browser_type"].assess({"element_id": search_id, "text": "react", "submit": True})[0] == SAFE
    out = run_tool("browser_type", {"element_id": search_id, "text": "react", "submit": True})
    assert "searched react" in out

    run_tool("browser_snapshot", {})
    assert "Typed" in run_tool("browser_type", {"element_id": _id("Full name"), "text": "Asha Rao"})
    assert "Selected" in run_tool("browser_select", {"element_id": _id("Country"), "option": "India"})
    run_tool("browser_check", {"element_id": _id("I agree"), "checked": True})
    cv = tmp_path / "resume.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    assert "file resume.pdf" in run_tool("browser_upload", {"element_id": _id("Resume"), "file_path": str(cv)})

    run_tool("browser_snapshot", {})
    run_tool("browser_type", {"element_id": _id("Cover letter"), "text": "Dear team,\nI love building UIs."})
    text = run_tool("browser_read_text", {})
    assert "I love building UIs." in text

    snap = run_tool("browser_click", {"text": "Next"})
    assert "Step 2 of 3" in snap
    assert 'value="Asha Rao"' in snap and "[x]" in snap and 'value="India"' in snap

    submit_id = _id("Submit application")
    assert TOOLS["browser_click"].assess({"element_id": submit_id})[0] == CRITICAL
    assert "SUBMITTED" in run_tool("browser_click", {"element_id": submit_id})

    filtered = run_tool("browser_snapshot", {"filter": "more jobs"})
    assert "More jobs" in filtered and "Full name" not in filtered.split("Page text")[0]
    page2 = run_tool("browser_click", {"text": "More jobs"})
    assert "Title: Page 2" in page2
    assert "Title: Apply - Test Co" in run_tool("browser_back", {})
    tabs = run_tool("browser_tabs", {"action": "list"})
    assert "Apply - Test Co" in tabs
    shot = run_tool("browser_screenshot", {})
    assert shot.startswith("Saved screenshot") and shot.split(": ", 1)[1].strip().endswith(".png")
    assert "gone" in run_tool("browser_click", {"element_id": 9999}) or "ERROR" in run_tool("browser_click", {"element_id": 9999})


def test_social_compose_builds_prefilled_url(site, monkeypatch):
    opened = []
    monkeypatch.setattr(browser.session, "call", lambda fn, *a: opened.append(a) or "snapshot")
    out = run_tool("social_compose", {"platform": "x", "text": "Launching MyApp v2 & dark mode!"})
    assert opened[0][0] == "https://x.com/intent/post?text=Launching+MyApp+v2+%26+dark+mode%21"
    assert "NEXT:" in out
    run_tool("social_compose", {"platform": "reddit", "text": "body", "title": "My title", "subreddit": "webdev"})
    assert opened[1][0].startswith("https://www.reddit.com/r/webdev/submit?type=TEXT&title=My+title")
