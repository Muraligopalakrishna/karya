"""Uploads where the page has no file field until its Upload button is clicked (Google Flow, 2026-10-18).

Karya clicks a real upload button, takes the file picker it opens and puts the file in. It never clicks a button
that could send, post or submit just to upload a file.
"""
import asyncio
import json
import shutil
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from karya import ext_link
from karya.tools import browser


@pytest.fixture()
def logo(tmp_path):
    f = tmp_path / "karya_logo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    return f


@pytest.fixture()
def ext(monkeypatch):
    """The extension session with a stand-in link that records what Karya asked for."""
    calls, answers = [], {}

    def request(method, params=None, timeout=45.0):
        calls.append((method, dict(params or {})))
        return answers.get(method, {"ok": True})

    monkeypatch.setattr(ext_link.link, "request", request)
    s = browser.ext_session
    monkeypatch.setattr(s, "items", {})
    monkeypatch.setattr(s, "url", "https://labs.google/fx/tools/flow/project/1")
    monkeypatch.setattr(s, "_settle", lambda ms=600: None)
    monkeypatch.setattr(s, "_snapshot", lambda **kw: "URL: https://labs.google/fx/tools/flow/project/1")
    return s, calls, answers


def test_upload_never_clicks_a_send_or_submit_button(ext, logo):
    s, calls, _ = ext
    for label in ("Submit application", "Post", "Upload & Apply", "Send file"):
        s.items = {7: {"id": 7, "tag": "button", "label": label}}
        out = s.upload(7, str(logo))
        assert out.startswith("NOT UPLOADED") and "isn't an upload button" in out, label
    assert calls == []                      # nothing was asked of Chrome at all


def test_an_unlabeled_submit_button_is_never_clicked_to_upload(ext, logo):
    s, calls, _ = ext
    s.items = {8: {"id": 8, "tag": "button", "type": "submit", "label": ""}}
    out = s.upload(8, str(logo))
    assert out.startswith("NOT UPLOADED") and "element 8" in out and calls == []


def test_upload_button_may_open_the_page_picker(ext, logo):
    s, calls, answers = ext
    s.items = {2500: {"id": 2500, "tag": "div", "label": "upload Upload"}}
    answers["upload_native"] = {"ok": True, "native": True, "picker": True, "target": "upload Upload"}
    out = s.upload(2500, str(logo))
    assert out.startswith("Uploaded karya_logo.png")
    method, params = calls[0]
    assert method == "upload_native" and params["click"] is True and params["path"] == str(logo.resolve())


def test_a_real_file_field_is_never_clicked(ext, logo):
    s, calls, _ = ext
    s.items = {3: {"id": 3, "tag": "input", "type": "file", "label": "Resume"}}
    assert s.upload(3, str(logo)).startswith("Uploaded")
    assert calls[0][1]["click"] is False


def test_no_picker_and_no_field_is_a_clear_not_uploaded(ext, logo):
    s, calls, answers = ext
    s.items = {9: {"id": 9, "tag": "div", "label": "New collection"}}
    answers["upload_native"] = {"ok": False, "no_input": True,
                                "error": "clicking \"New collection\" didn't open a file picker, and the page has no file field"}
    out = s.upload(9, str(logo))
    assert out.startswith("NOT UPLOADED: clicking \"New collection\" didn't open a file picker")
    assert "tell the user instead of trying again" in out
    assert [m for m, _ in calls] == ["upload_native"]     # no pointless page-script retry


def test_a_picker_only_the_user_can_answer_is_said_plainly(ext, logo):
    s, calls, answers = ext
    s.items = {5: {"id": 5, "tag": "button", "label": "Upload"}}
    answers["upload_native"] = {"ok": False, "user_needed": True,
                                "error": "\"Upload\" opens Chrome's newer file picker, which Karya can't fill in"}
    out = s.upload(5, str(logo))
    assert out.startswith("NOT UPLOADED") and "Don't try again" in out and "karya_logo.png" in out
    assert [m for m, _ in calls] == ["upload_native"]


def test_karyas_own_browser_wont_click_submit_to_upload(monkeypatch, logo):
    s = browser.BrowserSession()
    clicked = []

    class Loc:
        def click(self, **kw):
            clicked.append(kw)

    class Page:
        url = "https://boards.greenhouse.io/acme/jobs/1"

    monkeypatch.setattr(s, "_ensure", lambda: None)
    monkeypatch.setattr(s, "page", Page(), raising=False)
    monkeypatch.setattr(s, "_locator", lambda eid: (Loc(), {"id": eid, "tag": "button", "label": "Submit application"}))
    out = s.upload(4, str(logo))
    assert out.startswith("NOT UPLOADED") and "isn't an upload button" in out and clicked == []


def test_an_outdated_extension_is_asked_to_reload_once(monkeypatch):
    link = ext_link.ExtensionLink()
    loop = asyncio.new_event_loop()
    try:
        link.ws, link.loop, link.info = object(), loop, {"version": "0.0.1"}
        assert link.status()["needs_reload"]
        assert link.should_reload() is True
        assert link.should_reload() is False        # the same old version again: no reload loop
        link.info = {"version": ext_link.disk_version()}
        assert link.should_reload() is False        # up to date
    finally:
        loop.close()


# ---------------------------------------------------------------- the real extension (background.js) in Edge
FLOW = """<html><body><h1>Project</h1>
<div role="menu"><div role="menuitem" tabindex="0" style="padding:8px;width:140px" id="up">upload Upload</div>
<div role="menuitem" tabindex="0" style="padding:8px;width:140px">New collection</div></div>
<p id="out">nothing yet</p>
<script>
  document.getElementById('up').addEventListener('click', () => {
    const input = document.createElement('input');   // like Google Flow: made on click, never in the page
    input.type = 'file'; input.accept = 'image/*';
    input.addEventListener('change', () => { const f = input.files[0];
      document.getElementById('out').textContent = 'got ' + f.name + ' ' + f.size + ' bytes'; });
    input.click();
  });
</script></body></html>"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_real_extension_uploads_through_the_page_picker(tmp_path, logo):
    edge = [p for p in (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")) if p.exists()]
    if not edge:
        pytest.skip("Microsoft Edge isn't installed")
    from playwright.async_api import async_playwright
    from websockets.asyncio.server import serve

    ext = tmp_path / "ext"
    shutil.copytree(Path(browser.__file__).resolve().parents[1] / "extension", ext)
    site = tmp_path / "site"
    site.mkdir()
    (site / "flow.html").write_text(FLOW, encoding="utf-8")
    ws_port, http_port = _free_port(), _free_port()
    (ext / "config.json").write_text(json.dumps({"port": ws_port, "token": "test-token"}))   # never the real Karya
    httpd = ThreadingHTTPServer(("127.0.0.1", http_port), partial(SimpleHTTPRequestHandler, directory=str(site)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    out = {}

    async def run():
        conns = asyncio.Queue()

        async def handler(ws):
            await conns.put((ws, json.loads(await ws.recv())))
            await ws.wait_closed()

        async with serve(handler, "127.0.0.1", ws_port):
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    str(tmp_path / "profile"), channel="msedge", headless=True,
                    args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
                try:
                    try:
                        ws, hello = await asyncio.wait_for(conns.get(), 30)
                    except asyncio.TimeoutError:
                        out["skip"] = "Edge didn't load the unpacked extension"
                        return
                    n = 0

                    async def call(method, **params):
                        nonlocal n
                        n += 1
                        await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                        while True:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), 40))
                            if msg.get("id") == n:
                                return msg.get("result", msg)

                    await call("open", url=f"http://127.0.0.1:{http_port}/flow.html", new_tab=True)
                    items = (await call("snapshot")).get("items") or []
                    ids = {i.get("label", ""): i.get("id") for i in items}
                    up = next(v for k, v in ids.items() if "Upload" in k)
                    other = next(v for k, v in ids.items() if "collection" in k)
                    out["no_click"] = await call("upload_native", id=up, path=str(logo), click=False)
                    out["picker"] = await call("upload_native", id=up, path=str(logo), click=True)
                    out["page"] = (await call("text", max_chars=400)).get("text", "")
                    out["dead"] = await call("upload_native", id=other, path=str(logo), click=True)
                    out["version"] = hello.get("version")
                    # Karya was updated: a newer version is on disk, and the running one reloads itself into it
                    manifest = ext / "manifest.json"
                    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
                        f'"version": "{out["version"]}"', '"version": "9.9.9"'), encoding="utf-8")
                    out["reload"] = await call("reload")
                    out["again"] = (await asyncio.wait_for(conns.get(), 20))[1].get("version")
                finally:
                    await ctx.close()

    try:
        asyncio.run(run())
    finally:
        httpd.shutdown()
    if out.get("skip"):
        pytest.skip(out["skip"])
    assert out["no_click"]["ok"] is False and out["no_click"]["no_input"]     # not allowed to click: nothing clicked
    assert out["picker"] == {"ok": True, "native": True, "picker": True, "target": "upload Upload"}
    assert "got karya_logo.png 108 bytes" in out["page"]
    assert out["dead"]["ok"] is False and "didn't open a file picker" in out["dead"]["error"]
    assert out["reload"]["ok"] and out["again"] == "9.9.9"                # back by itself, as the new version
