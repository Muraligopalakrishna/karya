"""Karya as an MCP server: another AI app (Claude, Cursor, Codex, Kiro...) drives Karya's tools with its own model.

The end-to-end tests start the real `karya_mcp.py` as a subprocess, talk JSON-RPC over stdio exactly like an MCP client,
and run a real Karya server behind it. Every safety check of Karya's own chat must still apply."""
import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from karya import outbox
from karya.config import ROOT, settings
from karya.registry import CRITICAL, P, TOOLS, tool
from karya.tools import browser, email_tools

from .conftest import FakeLLM

TOKEN = "mcp-test-token-0123456789abcdef"
RAN = []


@tool("_mcp_critical", "test: a send/post-like action", {"x": P("string", "x")}, risk=CRITICAL)
def _mcp_critical(x: str = ""):
    RAN.append(x)
    return f"Posted {x}."


@tool("_mcp_echo", "test: a safe action", {"x": P("string", "x")})
def _mcp_echo(x: str = ""):
    return f"echo {x}"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def karya_server():
    import uvicorn

    from karya.agent import Agent
    from karya.server import create_app
    RAN.clear()
    port = _free_port()
    agent = Agent(llm=FakeLLM([]), persist=False)
    app = create_app(agent=agent, token=TOKEN, port=port)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port, app
    srv.should_exit = True
    thread.join(5)


class Client:
    """A minimal MCP client: starts karya_mcp.py and talks newline-delimited JSON-RPC over stdio."""

    def __init__(self, port, elicitation=False, answer=None, tools=""):
        env = {**os.environ, "KARYA_MCP_URL": f"http://127.0.0.1:{port}", "KARYA_MCP_TOKEN": TOKEN,
               "KARYA_MCP_NO_START": "1", "KARYA_MCP_TOOLS": tools, "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "karya_mcp.py")], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(ROOT))
        self.answer = answer          # how the "user" answers elicitation in the app
        self.asked = []
        self.notes = []
        self.inbox = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        caps = {"elicitation": {}} if elicitation else {}
        self.init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": caps,
                                                "clientInfo": {"name": "test-client", "version": "1"}})
        self.notify("notifications/initialized")

    def _read(self):
        for raw in self.proc.stdout:
            msg = json.loads(raw)
            if msg.get("method") == "elicitation/create":  # the server asks the user, inside the "app"
                self.asked.append(msg["params"])
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": self.answer(msg["params"])})
            elif msg.get("method", "").startswith("notifications/"):
                self.notes.append(msg)
            else:
                self.inbox.put(msg)

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def notify(self, method, params=None):
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None, rid=None, timeout=30):
        rid = rid or f"r{time.time_ns()}"
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg.get("id") == rid:
                return msg
        raise AssertionError(f"no answer to {method}")

    def call(self, name, args=None, **kw):
        msg = self.request("tools/call", {"name": name, "arguments": args or {}, "_meta": {"progressToken": "p1"}}, **kw)
        result = msg["result"]
        return result["content"][0]["text"], result.get("isError"), result

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(5)


def _approve_in_karya(app, approve=True, timeout=10):
    """The user clicks Approve/Deny on Karya's own card."""
    hub = app.state.hub
    end = time.time() + timeout
    while time.time() < end:
        for rid in list(hub.pending):
            hub.answer(rid, approve)
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- protocol
def test_resume_tools_get_karyas_own_ai_over_mcp(karya_server, monkeypatch):
    """Before the fix, Karya's own AI was only set up by its chat, so an app's first tailor_resume failed."""
    from karya import llm
    from karya.tools import resume
    port, app = karya_server

    class OwnAI(FakeLLM):
        providers = ["groq"]

        def complete(self, system, user):
            return json.dumps({"name": "Asha", "skills": ["SQL"], "summary": "Tailored by Karya's own AI"})
    app.state.hub.agent.llm = OwnAI([])
    monkeypatch.setattr(llm, "ACTIVE", None)                # Karya's own chat hasn't run since it started
    made = []
    monkeypatch.setattr(resume, "build_resume", lambda r, job_title="", company="": made.append(r) or {"pdf": "x.pdf"})
    resume.save_resume_data({"name": "Asha", "skills": ["SQL"], "summary": "Builder"})
    c = Client(port)
    try:
        text, error, _ = c.call("tailor_resume", {"job_title": "APM", "company": "Zeta", "job_description": "SQL"})
    finally:
        c.close()
    assert not error and "karya_needs" not in text and made[0]["summary"] == "Tailored by Karya's own AI"


def test_handshake_and_tool_list(karya_server):
    port, _ = karya_server
    c = Client(port)
    try:
        init = c.init["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "karya"
        assert init["capabilities"]["tools"] and "RESULT: SUBMITTED" in init["instructions"][:512]
        tools = {t["name"]: t for t in c.request("tools/list")["result"]["tools"]}
        assert 25 <= len(tools) <= 40                                            # fits Cursor's ~40-tool limit
        assert {"browser_open", "browser_click", "send_email", "social_compose", "karya_activity",
                "browser_click_at"} <= set(tools)
        assert "enable_tools" not in tools and "task_status" not in tools
        assert tools["browser_snapshot"]["annotations"]["readOnlyHint"] is True
        assert tools["send_email"]["inputSchema"]["properties"]["to"]["type"] == "array"
        unknown = c.request("server/discover")                                  # a 2026 client's probe: fall back
        assert unknown["error"]["code"] == -32601
        assert c.request("ping")["result"] == {}
        assert c.request("prompts/list")["result"] == {"prompts": []}
        newer = Client.__new__(Client)                                          # unknown newer version -> ours
        assert init["protocolVersion"] in ("2025-11-25", "2025-06-18")
    finally:
        c.close()


def test_selecting_tools(karya_server):
    port, _ = karya_server
    c = Client(port, tools="browser,email")
    try:
        names = {t["name"] for t in c.request("tools/list")["result"]["tools"]}
        assert "send_email" in names and "browser_drag" in names and "find_jobs" not in names
    finally:
        c.close()


# ---------------------------------------------------------------- approvals
def test_approval_in_karyas_window_when_the_app_cant_ask(karya_server, monkeypatch):
    port, app = karya_server
    monkeypatch.setattr("karya.mcp_bridge.McpRuns._show_karya", lambda self: None)
    c = Client(port, tools="_mcp_critical,_mcp_echo")
    try:
        assert c.call("_mcp_echo", {"x": "hi"})[0] == "echo hi"                  # safe: no approval
        threading.Thread(target=_approve_in_karya, args=(app, True), daemon=True).start()
        text, is_error, _ = c.call("_mcp_critical", {"x": "launch post"})
        assert text == "Posted launch post." and not is_error and RAN == ["launch post"]
        threading.Thread(target=_approve_in_karya, args=(app, False), daemon=True).start()
        text, is_error, _ = c.call("_mcp_critical", {"x": "second"})
        assert text.startswith("The user DENIED") and is_error and RAN == ["launch post"]
    finally:
        c.close()


def test_approval_inside_the_app(karya_server):
    port, app = karya_server
    answers = iter([{"action": "accept", "content": {"approve": True}}, {"action": "decline"}])
    c = Client(port, elicitation=True, answer=lambda params: next(answers), tools="_mcp_critical")
    try:
        text, _, _ = c.call("_mcp_critical", {"x": "approved in app"})
        assert text == "Posted approved in app." and "test-client" in c.asked[0]["message"]
        assert c.asked[0]["requestedSchema"]["properties"]["approve"]["type"] == "boolean"
        text, is_error, _ = c.call("_mcp_critical", {"x": "declined"})
        assert text.startswith("The user DENIED") and is_error
        assert RAN == ["approved in app"] and not app.state.hub.pending        # Karya's window wasn't needed
    finally:
        c.close()


def test_questions_only_the_user_knows_are_asked_in_the_app(karya_server):
    port, _ = karya_server
    c = Client(port, elicitation=True, answer=lambda params: {"action": "accept", "content": {"q0": "Immediately"}},
               tools="ask_user")
    try:
        text, _, _ = c.call("ask_user", {"questions": ["What is your notice period?"]})
        assert json.loads(text)["answers"] == {"What is your notice period?": "Immediately"}
        from karya import answers
        assert answers.saved_answer("Notice period") == "Immediately"
    finally:
        c.close()


def test_progress_keeps_long_waits_alive_and_cancel_denies(karya_server, monkeypatch):
    port, app = karya_server
    monkeypatch.setattr("karya.mcp_bridge.McpRuns._show_karya", lambda self: None)
    c = Client(port, tools="_mcp_critical")
    try:
        c.send({"jsonrpc": "2.0", "id": "slow", "method": "tools/call",
                "params": {"name": "_mcp_critical", "arguments": {"x": "never"}, "_meta": {"progressToken": "t9"}}})
        time.sleep(9)                                                           # waiting for the user...
        assert any(n["method"] == "notifications/progress" and n["params"]["progressToken"] == "t9" for n in c.notes)
        c.notify("notifications/cancelled", {"requestId": "slow"})              # the app gave up
        time.sleep(1.5)
        assert RAN == [] and not app.state.hub.pending                           # the card is gone, nothing ran
    finally:
        c.close()


# ---------------------------------------------------------------- Karya's checks apply to every AI
def test_guards_apply_to_other_ais(karya_server):
    port, _ = karya_server
    c = Client(port, tools="send_email,social_compose,karya_activity")
    try:
        text, is_error, _ = c.call("send_email", {"to": ["a@shop1.example", "b@shop2.example"], "subject": "x", "body": "y"})
        assert text.startswith("NOT SENT") and "several businesses" in text and is_error
        outbox.record(["owner@cafe.example"], "Website ideas", "sent")
        text, _, _ = c.call("send_email", {"to": ["owner@cafe.example"], "subject": "x", "body": "y"})
        assert "already got your email" in text
        outbox.mark_bounced("gone@nowhere.example")
        assert "bounced before" in c.call("send_email", {"to": ["gone@nowhere.example"], "subject": "x", "body": "y"})[0]
        outbox.record_post("x.com", "Launching SkillCandle replay drills today for every trader")
        text, _, _ = c.call("social_compose", {"platform": "x", "text": "Launching SkillCandle replay drills today for every trader"})
        assert text.startswith("NOT OPENED") and "already posted" in text
        activity = json.loads(c.call("karya_activity")[0])
        assert activity["emails"][0]["to"] == "owner@cafe.example" and activity["posts"][0]["site"] == "x.com"
        assert activity["left_today"]["emails"] == settings.max_emails_per_day - 1
    finally:
        c.close()


def test_daily_limits(monkeypatch):
    monkeypatch.setattr(settings, "max_emails_per_day", 2)
    outbox.record(["a@one.example"], "s", "sent")
    outbox.record(["b@two.example"], "s", "sent")
    outbox.note_seen("c@three.example")
    stop = email_tools.send_precheck({"to": ["c@three.example"], "subject": "x", "body": "y"})
    assert stop.startswith("NOT SENT: today's limit of 2 emails")
    monkeypatch.setattr(settings, "max_posts_per_day", 1)
    outbox.record_post("www.linkedin.com", "first post of the day about replay practice")
    assert browser._compose_precheck({"platform": "linkedin", "text": "another one"}).startswith("NOT OPENED: today's limit")


def test_unseen_addresses_from_an_app_get_a_warning_not_a_block():
    args = {"to": ["friend@startup.example"], "subject": "Hi", "body": "Hello", "_via": "mcp"}
    assert email_tools.send_precheck(args) is None                            # the user may have typed it in the app
    level, card = email_tools._send_summary(args)
    assert level == CRITICAL and "WARNING: Karya never saw friend@startup.example" in card
    assert "may be made up" in email_tools.send_precheck({**args, "_via": ""})  # Karya's own AI: blocked


def test_karyas_chat_waits_while_an_app_works(karya_server):
    port, app = karya_server
    from fastapi.testclient import TestClient  # noqa: F401 - just making sure the server keeps running
    app.state.hub.mcp_client = "Cursor"
    import websockets

    async def chat():
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}", origin=f"http://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "chat", "text": "find jobs"}))
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if ev["type"] == "error":
                    return ev["text"]
    result = []
    t = threading.Thread(target=lambda: result.append(asyncio.run(chat())))
    t.start()
    t.join(10)
    app.state.hub.mcp_client = None
    assert result and result[0].startswith("Cursor is using Karya right now")


def test_bridge_endpoints_need_the_token_and_refuse_web_pages(karya_server):
    import urllib.error
    import urllib.request
    port, _ = karya_server

    def get(headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/mcp/info", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
    assert get({}) == 401
    assert get({"Authorization": "Bearer wrong"}) == 401
    assert get({"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"}) == 401   # a web page
    assert get({"Authorization": f"Bearer {TOKEN}"}) == 200


def test_mcp_switch_off(karya_server, monkeypatch):
    port, _ = karya_server
    monkeypatch.setattr(settings, "mcp_enabled", False)
    c = Client(port)
    try:
        err = c.request("tools/list")["error"]
        assert "switched off" in err["message"]
    finally:
        c.close()


# ---------------------------------------------------------------- canvas and chess on a real page
BOARD = """<!doctype html><html><head><title>Chess</title><style>
body { margin: 0; } #board { display: block; position: relative; width: 400px; height: 400px; margin: 20px; background: #b58863; }
.piece { position: absolute; width: 50px; height: 50px; } canvas { display: block; margin: 20px; border: 1px solid #000; }
</style></head><body>
<wc-chess-board id="board"></wc-chess-board>
<canvas id="pad" width="300" height="200"></canvas>
<p id="log">ready</p>
<p id="draglog">no drag</p>
<script>(() => {
  const board = document.getElementById('board');
  const start = {'52': 'wp', '57': 'bp', '51': 'wk', '58': 'bk'};
  function draw() {
    board.innerHTML = '';
    for (const [sq, kind] of Object.entries(start)) {
      const p = document.createElement('div'); p.className = `piece ${kind} square-${sq}`;
      p.style.left = ((Number(sq[0]) - 1) * 50) + 'px'; p.style.top = ((8 - Number(sq[1])) * 50) + 'px';
      board.appendChild(p);
    }
  }
  draw();
  let picked = null;
  const sqAt = (e) => { const r = board.getBoundingClientRect();
    return `${Math.floor((e.clientX - r.left) / 50) + 1}${8 - Math.floor((e.clientY - r.top) / 50)}`; };
  board.addEventListener('pointerdown', (e) => {
    const sq = sqAt(e);
    if (picked && picked !== sq && start[picked] && start[picked][0] === 'w') {   // click-click move (white only)
      start[sq] = start[picked]; delete start[picked]; picked = null; draw(); return;
    }
    picked = start[sq] ? sq : null;
  });
  const pad = document.getElementById('pad');
  pad.addEventListener('click', (e) => { const r = pad.getBoundingClientRect();
    document.getElementById('log').textContent = `clicked ${Math.round(e.clientX - r.left)},${Math.round(e.clientY - r.top)}`; });
  let down = null;
  pad.addEventListener('mousedown', (e) => { down = [e.clientX, e.clientY]; });
  pad.addEventListener('mouseup', (e) => { if (down && Math.abs(e.clientX - down[0]) > 20)
    document.getElementById('draglog').textContent = `dragged ${Math.round(e.clientX - down[0])}`; down = null; });
})();</script></body></html>"""


@pytest.fixture()
def board_page(tmp_path):
    import functools
    import http.server
    (tmp_path / "board.html").write_text(BOARD, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/board.html"
    srv.shutdown()


def test_canvas_and_chess_in_karyas_own_window(board_page, monkeypatch):
    session = browser.BrowserSession()
    session.profile_dir = Path(settings.workspace) / "profile"
    monkeypatch.setattr(browser, "session", session)
    try:
        snap = browser.browser_open(board_page)
        assert "CHESS BOARD (chess.com; White is at the bottom)" in snap and "White: Ke1 Pe2" in snap
        assert "Black: Ke8 Pe7" in snap and "area canvas" in snap
        canvas = next(it for it in session.items.values() if it.get("area") and it["tag"] == "canvas")
        out = browser.browser_click_at(0.5, 0.5, element_id=canvas["id"])
        import re as _re
        x, y = map(int, _re.search(r"clicked (\d+),(\d+)", session.call(session.all_text)).groups())
        assert abs(x - 151) <= 1 and abs(y - 101) <= 1                          # the centre of the 302x202 box
        browser.browser_drag(0.1, 0.5, 0.6, 0.5, element_id=canvas["id"])
        dragged = int(_re.search(r"dragged (\d+)", session.call(session.all_text)).group(1))
        assert abs(dragged - 151) <= 2                                          # half the box width
        moved = browser.browser_move_piece("e2", "e4")
        assert moved.startswith("Moved e2-e4") and "Pe4" in moved
        bad = browser.browser_move_piece("e7", "e5")                              # black's piece: the page refuses
        assert bad.startswith("NOT MOVED") and "didn't happen" in bad
        assert browser.browser_move_piece("a3", "a4").startswith("NOT MOVED: there's no piece on a3")
        assert TOOLS["browser_click_at"].assess({"x": 0.5, "y": 0.5, "element_id": canvas["id"]})[0] == "safe"
        assert TOOLS["browser_click_at"].assess({"x": 5, "y": 5})[0] == CRITICAL   # unknown spot on the page
        assert out.startswith("Clicked at")
    finally:
        session.shutdown()
