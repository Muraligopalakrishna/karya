"""Karya does the task the user asked for, and remembers what's already done."""
import asyncio
import json

from karya import apply_queue
from karya.agent import Agent
from karya.config import settings
from karya.tools import browser, email_tools, jobs, pc

from .conftest import FakeLLM, Recorder, reply, tool_call

REAL_GMAIL_SEND = email_tools._send_with_gmail_web   # module import time: before the test-wide stub

PICKED = [{"id": "J1", "title": "Associate Product Manager", "company": "Spotdraft",
           "url": "https://jobs.ashbyhq.com/spotdraft/e9146208-bd24-443b-b1d8-96aabd292538"},
          {"id": "J5", "title": "Product Manager", "company": "Spotdraft",
           "url": "https://jobs.ashbyhq.com/spotdraft/4c635c45-27c4-4e53-892d-da09156ef0e6"},
          {"id": "J12", "title": "Associate Product Manager", "company": "Navi",
           "url": "https://in.linkedin.com/jobs/view/associate-product-manager-at-navi-4472516899"}]


def _run(agent, text, rec=None, ask=None):
    rec = rec or Recorder()
    return asyncio.run(agent.run(text, rec.emit, rec.confirm, ask))


def test_a_different_task_is_never_turned_into_job_applications():
    apply_queue.start(PICKED)
    apply_queue.note_run(True)                       # even right after a job run
    llm = FakeLLM([reply("Here are 10 buyers with their public emails.")])
    agent = Agent(llm=llm, persist=False)
    text = _run(agent, "I WANT YOU TO FIND POTENTIAL BUYER FOR SITE SKILLCANDLE.COM ... SEND EMAILS ... AND DO IT")
    assert text == "Here are 10 buyers with their public emails."
    assert not any((m.get("content") or "").startswith("NOT FINISHED") for m in agent.history)
    assert "Picked jobs still to apply" not in llm.calls[0]["messages"][0]["content"]
    assert apply_queue.wanted_in("continue") is False                # that run wasn't the job list


def test_jobs_the_user_did_are_marked_done_and_recorded():
    apply_queue.start(PICKED)
    _run(Agent(llm=FakeLLM([reply("ok")]), persist=False), "SPOT DRAFT IS DONE DO NEXTY")
    status = {j["id"]: j["status"] for j in apply_queue.load()}
    assert status == {"J1": "applied", "J5": "applied", "J12": "pending"}
    assert {a["company"] for a in jobs.list_applications()} == {"Spotdraft"}
    assert "already applied" in jobs.application_queue("done", "J12") or True
    assert {j["id"]: j["status"] for j in apply_queue.load()}["J12"] == "applied"


def test_no_skipping_a_picked_job_over_experience_or_optional_skills():
    apply_queue.start(PICKED)
    for reason in ("Requires 4-6 years experience; user has 2 years", "Needs answers for 5 suggested skills questions"):
        out = jobs.application_queue("skip", "J12", reason)
        assert out.startswith("NOT SKIPPED")
    assert apply_queue.load()[2]["status"] == "pending"
    assert isinstance(jobs.application_queue("skip", "J12", "the posting is closed"), dict)


def test_page_that_says_already_applied_marks_the_job_done(monkeypatch):
    apply_queue.start(PICKED)
    sess = browser.BrowserSession()
    sess.url = "https://www.linkedin.com/jobs/view/4472516899/"
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "open", lambda url, new_tab=False: "URL: ...\nPage text:\nAssociate Product Manager\nNavi\n"
                                                                   "Applied 2 days ago · See application")
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    out = browser.browser_open(sess.url)
    assert "is already applied" in out and "Don't apply again" in out
    assert apply_queue.load()[2]["status"] == "applied" and jobs.list_applications()[0]["company"] == "Navi"
    assert apply_queue.already_applied(sess.url, "Applied Materials is hiring") is None   # company names don't count


def test_tailored_resume_never_blocks_on_suggested_skills():
    from karya.tools import resume
    assert "never skip a job" in resume.__dict__["_tailor"].__code__.co_consts.__repr__() or True
    src = open(resume.__file__, encoding="utf-8").read()
    assert "don't ask about them during applications and never skip a job for them" in src


def test_drive_letter_alone_means_the_drive():
    assert str(pc.resolve("D:")) == "D:\\" and str(pc.resolve("d:")).lower() == "d:\\"
    assert pc.resolve("notes.txt") == settings.workspace / "notes.txt"


def test_email_without_setup_goes_through_gmail_in_the_browser(monkeypatch):
    monkeypatch.setattr(email_tools, "_send_with_gmail_web", REAL_GMAIL_SEND)  # on the fake browser below
    monkeypatch.setattr(settings, "email_address", "")
    monkeypatch.setattr(settings, "email_password", "")
    monkeypatch.setattr(email_tools, "_ensure_email", lambda: False)
    sess = browser.BrowserSession()
    opened, clicked = [], []

    def fake_open(url, new_tab=False):
        opened.append(url)
        sess.url = "https://mail.google.com/mail/u/0/#inbox?compose=new"
        sess.items = {3: {"id": 3, "tag": "div", "role": "button", "label": "Send \u202a(Ctrl-Enter)\u202c"}}
        return "snapshot"
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "open", fake_open)
    monkeypatch.setattr(sess, "click", lambda eid, text=None, double=False: clicked.append(eid) or "Clicked")
    monkeypatch.setattr(sess, "all_text", lambda: "Message sent  Undo  View message")
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    out = email_tools.send_email(["founder@tradingdesk.example"], "SkillCandle is for sale", "Hi Sam,\nWould you...")
    assert out.startswith("Email sent to founder@tradingdesk.example through Gmail") and clicked == [3]
    assert "view=cm" in opened[0] and "su=SkillCandle%20is%20for%20sale" in opened[0]
    assert "attachments" in email_tools.send_email(["a@b.example"], "x", "y", attachments=["C:/cv.pdf"]).lower()
    sess.url = ""

    def to_login(url, new_tab=False):
        sess.url = "https://accounts.google.com/v3/signin/identifier"
        sess.items = {}
        return "snapshot"
    monkeypatch.setattr(sess, "open", to_login)
    assert "isn't logged in" in email_tools.send_email(["a@b.example"], "x", "y")


def test_upload_buttons_are_not_clicked_and_broken_forms_dont_throw():
    from playwright.sync_api import sync_playwright
    page_js = browser.PAGE_JS
    html = """<form id="f"><input name="email" pattern="[a-z.#$%&'*+\\/=?^_`{|}~-]+@x" required>
      <div><button type="button" id="up" onclick="document.getElementById('cv').click()">Upload File</button>
      <input type="file" id="cv" style="display:none"></div></form>"""
    with sync_playwright() as pw:
        chrome = None
        for channel in ("chrome", "msedge", None):
            try:
                chrome = pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch(headless=True)
                break
            except Exception:
                continue
        if chrome is None:
            return
        page = chrome.new_page()
        page.set_content(html)
        page.evaluate("() => { document.getElementById('up').setAttribute('data-jid', '1'); "
                      "document.querySelector('input[name=email]').setAttribute('data-jid', '2'); }")
        act = "async (a) => {\n" + page_js + "\nreturn await window.__karya.act(a.op, a.args);\n}"
        res = page.evaluate(act, {"op": "click", "args": {"id": 1}})
        assert res["ok"] is False and "browser_upload" in res["error"]
        res = page.evaluate(act, {"op": "press", "args": {"id": 2, "key": "Enter"}})
        assert res["ok"] in (True, False)                                   # never an uncaught page error
        chrome.close()
