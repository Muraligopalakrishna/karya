"""Login codes (karya/login_codes.py, enter_login_code) and pop-ups (page.js closePopups / popups)."""
import asyncio
import json
import time
from datetime import datetime

import pytest

from karya import login_codes, phone, secrets_filter
from karya.agent import Agent
from karya.registry import run_tool
from karya.tools import browser

from .conftest import FakeLLM, reply, tool_call


# ---------------------------------------------------------------- reading codes
@pytest.mark.parametrize("subject,body,want", [
    ("482913 is your Instagram code", "", "482913"),
    ("Verify your account", "Hi Asha,\nEnter this security code:\n\n331 902\n\nThanks, The Instagram team", None),
    ("Your OTP for Cutshort is 5629", "", "5629"),
    ("Cutshort login", "Your one time password is 5629. It is valid for 10 minutes. © 2026 Cutshort", "5629"),
    ("G-771204 is your Google verification code", "", "771204"),
    ("Your verification code", "Use 650113 to verify your email address", "650113"),
    ("Your sign-in code", "Your code is: K7Q2ZP", "K7Q2ZP"),
    ("Your order", "Total ₹1,299. Call 1800-208-9898. Order 2026-10-11", None),
    ("Security alert", "We noticed a new login on 11/10/2026 at 01:14. © 2026 Example", None),
])
def test_extract_code(subject, body, want):
    assert login_codes.extract_code(subject, body) == want


def test_split_digits_with_a_space_are_read_too():
    assert login_codes.extract_code("", "Your security code: 331902") == "331902"


@pytest.mark.parametrize("sender,host,ok", [
    ("Instagram <security@mail.instagram.com>", "www.instagram.com", True),
    ("Facebook <security@facebookmail.com>", "www.instagram.com", True),
    ("Cutshort <noreply@cutshort.io>", "cutshort.io", True),
    ("Cutshort <otp@mail.cutshort.co>", "cutshort.io", True),          # its mail domain differs, same name
    ("Instagram <security@instagram-security.com>", "www.instagram.com", False),
    ("Instagram Support <help@evil.example>", "www.instagram.com", False),
    ("LinkedIn <security-noreply@linkedin.com>", "www.linkedin.com", True),
    ("Workday <no-reply@myworkday.com>", "kla.wd1.myworkdayjobs.com", True),
    ("", "cutshort.io", False),
])
def test_sender_must_be_the_same_site(sender, host, ok):
    assert login_codes.sender_ok(sender, host) is ok


def test_site_names_and_user_typed_codes():
    assert login_codes.site_name("www.instagram.com") == "instagram"
    assert login_codes.site_name("shop.example.co.in") == "example"
    assert login_codes.clean_code("123 456") == "123456"
    assert login_codes.clean_code("code is 5629") == "5629"
    assert login_codes.clean_code("abc") is None and login_codes.clean_code("") is None


def test_gmail_row_times():
    now = datetime(2026, 10, 11, 2, 0)
    assert login_codes._when("Sat, Oct 11, 2026, 1:14 AM", now) == datetime(2026, 10, 11, 1, 14).timestamp()
    assert login_codes._when("Sat, 11 Oct 2026, 01:14", now) == datetime(2026, 10, 11, 1, 14).timestamp()
    assert login_codes._when("yesterday", now) is None


def test_wait_for_code_prefers_a_newer_email(monkeypatch):
    asked = time.time()
    found = [("1111", asked - 600, "your Gmail")]                       # an old code first
    monkeypatch.setattr(login_codes, "find_code", lambda host, since: found[-1])
    monkeypatch.setattr(login_codes, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(login_codes, "NEWER_WAIT", 0.3)

    def later():
        time.sleep(0.1)
        found.append(("2222", asked + 5, "your Gmail"))
    import threading
    threading.Thread(target=later).start()
    assert login_codes.wait_for_code("x.com", asked, wait=2)[0] == "2222"
    found[:] = [("3333", asked - 600, "your Gmail")]                    # only an old one: used after a short wait
    assert login_codes.wait_for_code("x.com", time.time(), wait=2)[0] == "3333"
    monkeypatch.setattr(login_codes, "find_code", lambda host, since: None)
    assert login_codes.wait_for_code("x.com", time.time(), wait=0.05) is None


def test_codes_in_use_are_hidden_from_the_ai():
    login_codes.remember_active("482913")
    assert secrets_filter.scrub('[7] input "Security code" value="482913"') == '[7] input "Security code" value="******"'
    assert secrets_filter.scrub("order 14829130") == "order 14829130"                 # only the whole code


# ---------------------------------------------------------------- the agent asks when the email has no code
def test_no_code_in_email_asks_the_user_and_types_it(monkeypatch):
    monkeypatch.setattr(login_codes, "wait_for_code", lambda host, asked, wait=0, stop=None: None)
    monkeypatch.setattr(login_codes, "page_host", lambda: "cutshort.io")
    typed = []
    monkeypatch.setattr(login_codes, "type_code", lambda eid, code: typed.append((eid, code)) or "URL: x\nverified")
    llm = FakeLLM([tool_call("enter_login_code", {"element_id": 12}), reply("Logged in.")])
    agent = Agent(llm=llm, persist=False)
    asked = []

    async def ask(request):
        asked.append(request)
        return {"code": "56 29"}

    async def emit(event):
        return None

    async def confirm(request):
        return True
    assert asyncio.run(agent.run("log in to cutshort", emit, confirm, ask)) == "Logged in."
    assert asked[0]["kind"] == "code" and asked[0]["site"] == "Cutshort"
    assert typed == [(12, "5629")]
    result = next(m for m in agent.history if m.get("role") == "tool")["content"]
    assert result.startswith("Entered the code the user gave") and "5629" not in result


def test_the_code_from_email_is_typed_and_never_shown(monkeypatch, tmp_path):
    monkeypatch.setattr(login_codes, "page_host", lambda: "www.instagram.com")
    monkeypatch.setattr(login_codes, "wait_for_code",
                        lambda host, asked, wait=0, stop=None: ("482913", time.time() - 30, "your Gmail"))
    monkeypatch.setattr(browser, "_run", lambda method, *a: 'URL: x\n[12] input "Security code" value="482913"'
                        if method == "type_code" else "")
    out = run_tool("enter_login_code", {"element_id": 12})
    assert out.startswith("Entered the 6-character code from Instagram's email in your Gmail")
    assert "482913" not in secrets_filter.scrub(out)


# ---------------------------------------------------------------- WhatsApp
class Bridge:
    state = "ready"

    def __init__(self):
        self.out = []

    def send(self, text):
        self.out.append(text)


def test_whatsapp_asks_for_the_code_and_takes_only_a_code():
    from karya.server import Hub

    class Main:
        busy, auto_mode = False, False

    async def go():
        hub = Hub(Main())
        ch = phone.PhoneChannel(hub, None)
        ch.bridge = Bridge()
        future = asyncio.get_running_loop().create_future()
        hub.asks["k"] = (future, {})
        await ch.on_ask({"id": "k", "kind": "code", "site": "Cutshort", "bot": "Maya", "source": "assigned"})
        assert ch.bridge.out[-1].startswith("Maya asks: Cutshort sent you a one-time login code")
        await ch.on_message("hmm what")
        assert "send just the code" in ch.bridge.out[-1] and not future.done()
        await ch.on_message("5629")
        assert future.result() == {"code": "5629"}
    asyncio.run(go())


# ---------------------------------------------------------------- real Chrome: pop-ups, code boxes, Gmail
NAG_PAGE = """<!doctype html><html><body>
<h1>Jobs for you</h1><button id="apply">Apply</button>
<div role="dialog" aria-modal="true" style="position:fixed;top:20%;left:20%;width:60%;height:200px;background:#fff">
  <p>Are you still looking for a job?</p><button>Yes</button><button aria-label="Close" onclick="this.closest('[role=dialog]').remove()">×</button>
</div>
<div role="dialog" class="easy-apply" style="position:fixed;top:60%;left:10%;width:70%;height:150px;background:#eee">
  <h2>Apply to Acme</h2><label>Phone <input id="ph"></label><button aria-label="Dismiss" onclick="this.parentNode.remove()">x</button>
</div></body></html>"""

CODE_BOXES = """<!doctype html><html><body><p>Enter the code we sent to your email</p>
<div id="boxes" style="display:flex;gap:6px">
  <input maxlength="1" autocomplete="one-time-code" aria-label="Digit 1"><input maxlength="1" aria-label="Digit 2">
  <input maxlength="1" aria-label="Digit 3"><input maxlength="1" aria-label="Digit 4">
</div>
<script>
  const boxes = [...document.querySelectorAll('#boxes input')];
  boxes.forEach((b, i) => b.addEventListener('input', () => { if (b.value && boxes[i + 1]) boxes[i + 1].focus(); }));
  window.code = () => boxes.map((b) => b.value).join('');
</script></body></html>"""

GMAIL_LIKE = """<!doctype html><html><body><div role="main"><table><tbody>
<tr class="zA"><td><span email="otp@mail.cutshort.co" name="Cutshort">Cutshort</span></td>
  <td><span class="bog">Your OTP for Cutshort is 5629</span><span class="y2"> - valid for 10 minutes</span></td>
  <td class="xW"><span title="Sat, Oct 11, 2026, 1:14 AM">1:14 AM</span></td></tr>
<tr class="zA"><td><span email="news@cutshort.io">Cutshort</span></td>
  <td><span class="bog">10 new jobs for you</span><span class="y2"> - Product roles</span></td>
  <td class="xW"><span title="Fri, Oct 10, 2026, 9:00 AM">Oct 10</span></td></tr>
</tbody></table></div></body></html>"""


@pytest.fixture(scope="module")
def chrome_page():
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


def _call(pg, fn, args=None):
    return pg.evaluate(browser.PAGE_CALL_JS, {"fn": fn, "args": args or {}})


def test_nag_popups_are_closed_and_task_dialogs_are_kept(chrome_page):
    chrome_page.set_content(NAG_PAGE)
    closed = _call(chrome_page, "closePopups")["closed"]
    assert len(closed) == 1 and closed[0]["text"].startswith("Are you still looking for a job?")
    assert closed[0]["button"] == "Close"                                  # the X, not "Yes"
    assert chrome_page.evaluate("() => document.querySelectorAll('[role=dialog]').length") == 1   # Easy Apply stays
    _call(chrome_page, "snapshot", {"next": 1})
    left = _call(chrome_page, "popups")
    assert len(left) == 1 and left[0]["text"].startswith("Apply to Acme") and not left[0]["nag"]
    assert left[0]["close"] == "Dismiss" and left[0]["closeId"]
    lines = browser.popup_lines(left)
    assert lines[0].startswith('Pop-up open: "Apply to Acme') and f"browser_click [{left[0]['closeId']}]" in lines[0]


def test_a_code_split_into_boxes_is_typed_box_by_box(chrome_page):
    chrome_page.set_content(CODE_BOXES)
    items = {it["id"]: it for it in _call(chrome_page, "snapshot", {"next": 1})["items"]}
    first = next(it for it in items.values() if it.get("label") == "Digit 1")
    assert first["code"] and first["maxlength"] == 1
    boxes = browser.code_boxes(items, first["id"])
    assert len(boxes) == 4 and boxes[0] == first["id"]
    assert "(login code box: use enter_login_code)" in browser._fmt(first)
    for eid, ch in zip(boxes, "5629"):
        chrome_page.fill(f'[data-jid="{eid}"]', ch)
    assert chrome_page.evaluate("() => window.code()") == "5629"


def test_gmail_rows_are_read_for_the_code(chrome_page):
    chrome_page.set_content(GMAIL_LIKE)
    rows = _call(chrome_page, "gmailRows")["rows"]
    assert rows[0]["email"] == "otp@mail.cutshort.co" and rows[0]["when"] == "Sat, Oct 11, 2026, 1:14 AM"
    code = None
    for row in rows:
        if login_codes.sender_ok(row["email"], "cutshort.io"):
            code = login_codes.extract_code(row["subject"], row["snippet"])
            if code:
                break
    assert code == "5629"


GMAIL_OPEN = """<!doctype html><html><body><div role="main"><table><tbody>
<tr class="zA" id="otp"><td><span email="noreply@cutshort.io">Cutshort</span></td>
  <td><span class="bog">Asha, here's your OTP to login to your account on Cutshort</span>
      <span class="y2"> - Hi Asha, use the OTP below to log in.</span></td>
  <td class="xW"><span title="Sun, 11 Oct 2026, 01:14">1:14 AM</span></td></tr>
</tbody></table></div><div id="thread"></div>
<script>
  document.getElementById('otp').addEventListener('mouseup', () => {
    document.getElementById('thread').innerHTML = '<span class="gD" email="noreply@cutshort.io">Cutshort</span>' +
      '<span class="g3" title="Sun, 11 Oct 2026, 01:14">1:14 AM</span>' +
      '<div class="a3s">Hi Asha,<br>Use the OTP below to log in to Cutshort.<br><br><b>5629</b><br><br>' +
      'It expires in 10 minutes. &copy; 2026 Cutshort</div>';
  });
</script></body></html>"""


def test_a_code_only_inside_the_email_is_found_by_opening_it(chrome_page, monkeypatch):
    chrome_page.set_content(GMAIL_OPEN)
    calls = []

    def fake_run(method, *args):
        calls.append(method)
        if method == "crawl_read":
            return _call(chrome_page, args[2])
        if method == "crawl_act":
            return _call(chrome_page, args[0], args[1] if len(args) > 1 else {})
        return "closed"
    monkeypatch.setattr(browser, "_run", fake_run)
    monkeypatch.setattr(login_codes.time, "sleep", lambda s: None)
    got = login_codes.from_gmail_web("cutshort.io", since=datetime(2026, 10, 11, 1, 0).timestamp())
    assert got and got[0] == "5629"
    assert calls[0] == "crawl_read" and "crawl_act" in calls and calls[-1] == "crawl_close"
    late = login_codes.from_gmail_web("cutshort.io", since=datetime(2026, 10, 11, 2, 0).timestamp())
    assert late is None                                                  # too old: never used
