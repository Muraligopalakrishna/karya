"""End-to-end test of Karya Browser Link: loads karya/extension into a real Chromium, connects it to a real Karya
server and fills + submits a local application form through Karya's normal browser tools.

Branded Chrome/Edge no longer load unpacked extensions from the command line, so this needs Chrome for Testing or
Playwright's Chromium: set KARYA_TEST_CHROMIUM to its chrome.exe, unzip Chrome for Testing into
data/test_browser/chrome-win64, or run `playwright install chromium`. Skipped when none is available."""
import functools
import http.server
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from karya import ext_link
from karya.config import ROOT, settings
from karya.registry import TOOLS, run_tool
from karya.tools import browser

from .test_extension import FORM, _free_port

pytestmark = pytest.mark.browser
TOKEN = "e2e-token-0123456789abcdefghij"

FRAME = """<!doctype html><html><body><label for="city">Current city</label><input id="city">
<script>document.getElementById("city").addEventListener("input", (e) => { window.parent.__frameCity = e.target.value; });</script>
</body></html>"""
LINKS = """<!doctype html><html><head><title>Job board</title></head><body><h1>Jobs</h1>
<a href="apply2.html" target="_blank">Apply on company site</a></body></html>"""


def _launch(pw, ext_dir: Path, base: Path):
    args = [f"--disable-extensions-except={ext_dir}", f"--load-extension={ext_dir}"]
    attempts = []
    if os.environ.get("KARYA_TEST_CHROMIUM"):
        attempts.append(("KARYA_TEST_CHROMIUM", {"executable_path": os.environ["KARYA_TEST_CHROMIUM"]}))
    local = ROOT / "data" / "test_browser" / "chrome-win64" / "chrome.exe"
    if local.exists():
        attempts.append(("Chrome for Testing", {"executable_path": str(local)}))
    attempts.append(("Playwright Chromium", {"channel": "chromium"}))
    tried = []
    for n, (name, opts) in enumerate(attempts):
        try:
            ctx = pw.chromium.launch_persistent_context(str(base / f"profile{n}"), headless=True, args=args,
                                                        ignore_default_args=["--disable-extensions"], **opts)
        except Exception as exc:  # noqa: BLE001 - try the next browser
            tried.append(f"{name}: {str(exc).splitlines()[0][:120]}")
            continue
        try:
            worker = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=10000)
        except Exception:  # noqa: BLE001
            tried.append(f"{name}: the extension didn't load")
            ctx.close()
            continue
        return ctx, worker
    pytest.skip("no Chromium that can load unpacked extensions (" + "; ".join(tried) + ")")


@pytest.fixture(scope="module")
def linked(tmp_path_factory):
    import uvicorn
    from playwright.sync_api import sync_playwright

    from karya.agent import Agent
    from karya.server import create_app

    from .conftest import FakeLLM

    base = tmp_path_factory.mktemp("e2e")
    site_dir = base / "site"
    site_dir.mkdir()
    (site_dir / "apply2.html").write_text(FORM.replace(
        '<button type="submit"', '<iframe src="frame.html" title="More questions" style="width:420px;height:90px"></iframe>'
                                 '<button type="submit"'), encoding="utf-8")
    (site_dir / "frame.html").write_text(FRAME, encoding="utf-8")
    (site_dir / "links.html").write_text(LINKS, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site_dir))
    site = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()

    port = _free_port()
    app = create_app(agent=Agent(llm=FakeLLM([]), persist=False), token=TOKEN, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()

    ext_dir = base / "extension"  # a copy, so the real extension folder's config.json is never touched
    shutil.copytree(ext_link.EXTENSION_DIR, ext_dir, ignore=shutil.ignore_patterns("config.json"))
    ext_link.write_extension_config(port, TOKEN, folder=ext_dir)

    pw = sync_playwright().start()
    old_mode = settings.browser_mode
    ctx = None
    try:
        ctx, worker = _launch(pw, ext_dir, base)
        for _ in range(80):
            if ext_link.link.connected:
                break
            time.sleep(0.25)
        assert ext_link.link.connected, "the extension didn't connect to Karya"
        settings.browser_mode = "chrome"
        browser.ext_session.items, browser.ext_session.url = {}, ""
        yield {"ctx": ctx, "worker": worker, "site": f"http://127.0.0.1:{site.server_port}/", "base": base}
    finally:
        settings.browser_mode = old_mode
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001 - already closed by the last test
                pass
        pw.stop()
        server.should_exit = True
        site.shutdown()


@pytest.fixture(autouse=True)
def _use_the_extension(monkeypatch):
    monkeypatch.setattr(settings, "browser_mode", "chrome")   # these tests drive the real extension


def _find(part, tag=None):
    for it in browser.ext_session.items.values():
        if part.lower() in (it.get("label") or "").lower() and (tag is None or it.get("tag") == tag):
            return it
    raise AssertionError(f"{part} not found: {[i.get('label') for i in browser.ext_session.items.values()]}")


def _page(ctx, suffix):
    return next(p for p in ctx.pages if not p.is_closed() and p.url.endswith(suffix))


def test_fills_and_submits_in_its_own_tab(linked, tmp_path):
    ctx, worker, site = linked["ctx"], linked["worker"], linked["site"]
    users_tab = ctx.pages[0]
    users_tab.goto("data:text/html,<title>My own tab</title><p>mine</p>")
    snap = run_tool("browser_open", {"url": site + "apply2.html"})
    assert "(Karya's tab in your Chrome)" in snap and '"Name *"' in snap and "Current city" in snap, snap
    assert users_tab.url.startswith("data:")                 # the user's tab was not touched
    groups = worker.evaluate("async () => (await chrome.tabGroups.query({})).map((g) => g.title)")
    assert groups == ["Karya"]

    stop = TOOLS["browser_click"].precheck({"element_id": _find("Submit Application")["id"]})
    assert stop.startswith("NOT CLICKED") and "Resume" in stop and "B2B SaaS" in stop

    cv = tmp_path / "Asha_Resume_Acme.pdf"
    cv.write_bytes(b"%PDF-1.4 test resume")
    from karya import answers
    answers.save("Notice period", "30 days")
    answers.save("Do you have at least 2 years of B2B SaaS experience?", "No")
    answers.save("Location", "Hyderabad, India")
    fields = {_find("Name *")["id"]: "Asha Rao", _find("Email *")["id"]: "asha@example.org",
              _find("Location")["id"]: "Hyderabad", _find("No", "button")["id"]: "No",
              _find("Notice period")["id"]: "30 days", _find("Resume", "input")["id"]: str(cv),
              _find("Current city")["id"]: "Hyderabad"}
    out = run_tool("browser_fill", {"fields": {str(k): v for k, v in fields.items()}})
    assert "Filled 7 field(s)" in out and "Problems" not in out, out
    assert 'picked "Hyderabad, Telangana, India"' in out
    page = _page(ctx, "apply2.html")
    assert page.evaluate("() => window.__state") == {"name": "Asha Rao", "email": "asha@example.org",
                                                    "loc": "Hyderabad, Telangana, India", "yn": "No",
                                                    "file": "Asha_Resume_Acme.pdf"}
    assert page.evaluate("() => window.__frameCity") == "Hyderabad"   # field inside an iframe

    submit = _find("Submit Application")
    assert TOOLS["browser_click"].precheck({"element_id": submit["id"]}) is None
    result = run_tool("browser_click", {"element_id": submit["id"]})
    assert "RESULT: SUBMITTED" in result and "Thank you for applying" in result, result[:600]


def test_new_tab_links_tabs_screenshot_and_badge(linked):
    ctx, worker, site = linked["ctx"], linked["worker"], linked["site"]
    run_tool("browser_open", {"url": site + "links.html"})
    out = run_tool("browser_click", {"element_id": _find("Apply on company site")["id"]})
    assert "opened in a new Karya tab" in out and "Associate Product Manager - Acme" in out, out[:500]
    tabs = json.loads(run_tool("browser_tabs", {"action": "list"}))
    assert len(tabs) == 2 and tabs[1]["active"] and tabs[1]["url"].endswith("apply2.html")
    assert "Associate Product Manager at Acme" in run_tool("browser_read_text", {})
    shot = run_tool("browser_screenshot", {})
    assert shot.startswith("Saved screenshot") and Path(shot.split(": ", 1)[1].strip()).stat().st_size > 1000

    ext_link.link.notify("attention", {"on": True, "text": "Approve: Submit application"})
    time.sleep(0.6)
    assert worker.evaluate("() => chrome.action.getBadgeText({})") == "!"
    ext_link.link.notify("attention", {"on": False})
    time.sleep(0.6)
    assert worker.evaluate("() => chrome.action.getBadgeText({})") == ""

    assert "Closed Karya's tabs" in run_tool("browser_close", {})
    urls = worker.evaluate("async () => (await chrome.tabs.query({})).map((t) => t.url)")
    assert urls and not [u for u in urls if u.startswith(site)], urls   # Karya's tabs gone, the user's tab is still there


def test_closing_chrome_drops_the_link(linked):
    linked["ctx"].close()
    for _ in range(40):
        if not ext_link.link.connected:
            break
        time.sleep(0.25)
    assert not ext_link.link.connected
    out = run_tool("browser_open", {"url": linked["site"] + "links.html"})
    assert out.startswith("ERROR") and "isn't connected" in out   # 'only my Chrome' never falls back silently
