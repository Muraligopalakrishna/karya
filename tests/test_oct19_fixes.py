"""2026-10-19: "apply for 4" (meaning #4 in Maya's morning report, Neufin) opened Cisco from an older pick list, a job
the user had already applied to themselves. Then Instahyre's skills box took all skills as ONE skill (over its
50-character limit), and Karya filled it with the site's suggestions (Agile, Scrum, SQL) the user doesn't have."""
import asyncio
import json
import time

import pytest

from karya import answers, apply_queue, focus, job_lists
from karya.agent import Agent
from karya.tools import browser, jobs

from .conftest import FakeLLM, Recorder, reply

REPORT = """**Maya - Daily Job-Scan Report (Mon 19 Oct 09:00)**
I searched for product manager roles in India posted in the last 24 hours. Found 72 matching positions.
| Rank | Job Title | Company | City | Match | Link |
|------|-----------|---------|------|-------|------|
| 1 | Associate Product Manager | CapsLock | Bangalore | 85% | https://www.instahyre.com/job-443997-associate-product-manager-at-capslock-bangalore/ |
| 2 | Product Analyst | Zeta | Bangalore | 82% | https://jobs.lever.co/zeta/123 |
| 3 | APM | Acme | Pune | 81% | https://acme.example/careers/9 |
| 4 | Associate Product Manager | Neufin | Bangalore | 80% | https://www.instahyre.com/job-435987-associate-product-manager-at-neufin-bangalore/ |
"""
OLD_PICKS = [{"id": "J67", "title": "Engineering Product Manager", "company": "Cisco",
              "url": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/Bangalore-India/Engineering-Product-Manager_2021911-1"},
             {"id": "J76", "title": "Associate Product Manager", "company": "Neufin",
              "url": "https://www.instahyre.com/job-435987-associate-product-manager-at-neufin-bangalore/"}]


def _run(agent, text):
    rec = Recorder()
    return asyncio.run(agent.run(text, rec.emit, rec.confirm, None))


# ---------------------------------------------------------------- "apply for 4"
def test_a_report_becomes_a_numbered_list():
    rows = job_lists.from_report(REPORT)
    assert [r["n"] for r in rows] == [1, 2, 3, 4]
    assert rows[3]["company"] == "Neufin" and rows[3]["url"].startswith("https://www.instahyre.com/job-435987")
    assert job_lists.from_report("1. My video https://youtube.com/watch?v=1\n2. Another https://youtube.com/watch?v=2") == []


@pytest.mark.parametrize("text, nums", [
    ("apply for 4", [4]), ("apply the jobs it has given me this morning the 4 one", [4]), ("apply to 2 and 3", [2, 3]),
    ("apply 1,3", [1, 3]), ("submit #2", [2]), ("apply for 4th", [4]),
    ("apply to 4 jobs", []), ("apply to 10 companies", []), ("apply by 5 pm", []), ("apply to J4", []),
    ("find 4 jobs", []), ("apply to https://x.example/jobs/1", []), ("apply to all of them", [])])
def test_numbers_the_user_means(text, nums):
    assert job_lists.numbers_in(text) == nums


def test_resolve_uses_only_a_fresh_list_that_has_those_numbers():
    job_lists.record(job_lists.from_report(REPORT), "Maya's report")
    assert job_lists.resolve("apply for 4")[0]["company"] == "Neufin"
    assert job_lists.resolve("apply for 7") == []                 # not on the list: nothing is guessed
    data = json.loads(job_lists.FILE.read_text(encoding="utf-8"))
    data["time"] = time.time() - 2 * 86400
    job_lists.FILE.write_text(json.dumps(data), encoding="utf-8")
    assert job_lists.resolve("apply for 4") == []                 # two days old


def test_apply_for_4_means_the_report_not_the_old_pick_list():
    apply_queue.start(OLD_PICKS)
    agent = Agent(llm=FakeLLM([reply("ok")]), persist=False)
    agent.bot_report("Maya", REPORT)
    job_lists.record(job_lists.from_report(REPORT), "Maya's report")
    llm = agent.llm
    _run(agent, "apply for 4")
    system = llm.calls[0]["messages"][0]["content"]
    assert "#4 in Maya's report" in system and "Neufin" in system and "job-435987" in system
    assert "Picked jobs still to apply" not in system               # the old list doesn't take over
    assert agent._queue_task is False
    assert any(m.get("_bot") == "Maya" and "Neufin" in m["content"] for m in agent.history)   # the chat saw it
    assert "job-435987" in " ".join(answers.RECENT_USER[-2:])        # counts as the job the user named


def test_a_bot_report_is_added_before_the_next_message():
    agent = Agent(llm=FakeLLM([reply("ok")]), persist=False)
    agent.bot_report("Maya", REPORT)
    assert agent.history == []                                        # not in the middle of anything
    _run(agent, "which one fits me best?")
    roles = [(m["role"], m.get("_bot")) for m in agent.history[:2]]
    assert roles == [("assistant", "Maya"), ("user", None)]


def test_an_unknown_job_id_never_points_to_another_job():
    apply_queue.start(OLD_PICKS)
    answers.note_user_message("apply for 4")
    out = jobs.picked_only({"job_id": "J4"})
    assert out.startswith("NOT RUN") and "Cisco" not in out and "J67" not in out and "ask" in out
    answers.note_user_message("continue with my picks")              # carrying on with the picks: the next one
    assert "next: J67" in jobs.picked_only({"job_id": "J4"})


def test_both_cisco_jobs_can_be_marked_applied_by_the_user():
    apply_queue.start(OLD_PICKS)
    jobs.application_queue("done", "J67")
    assert {j["id"]: j["status"] for j in apply_queue.load()}["J67"] == "applied"


# ---------------------------------------------------------------- "posted in the last 24 hours" isn't a claim
def test_a_job_posting_date_is_not_a_claim_to_have_posted():
    assert focus.unverified_claim("I searched for PM roles in India posted in the last 24 hours.", [], []) is None
    assert focus.unverified_claim("Found 5 jobs posted 2 days ago.", [], []) is None
    assert focus.unverified_claim("Jobs posted on 18 Oct and posted within a week.", [], []) is None
    assert focus.unverified_claim("I posted your video on LinkedIn.", [], []) == "post"
    assert focus.unverified_claim("Your video was posted today.", [], []) == "post"      # still needs proof


# ---------------------------------------------------------------- skills boxes
BOX = {"id": 5129, "tag": "input", "type": "text", "label": "Suggested skills based on your role selected:"}


@pytest.fixture()
def master(monkeypatch):
    from karya.tools import resume
    data = {"name": "Test User", "skills": ["Problem Solving", "Product Strategy", "User Research", "Prompt Engineering"],
            "experience": [{"title": "Founder", "bullets": ["Ran product management for a marketplace"]}]}
    monkeypatch.setattr(resume, "load_master", lambda: data)
    return data


def test_only_the_users_own_skills_go_in(master):
    assert browser.skills_problem(BOX, "Product Strategy, User Research, Problem Solving") is None
    assert browser.skills_problem(BOX, "Product Management") is None          # it's in the resume text
    out = browser.skills_problem(BOX, "Agile, Product Strategy, Scrum, SQL")
    assert out.startswith("NOT TYPED") and "Agile, Scrum, SQL" in out and "Product Strategy" not in out.split("The user")[0]
    assert browser.skills_problem(BOX, "Agile Product Strategy Scrum SQL Prototyping").startswith("NOT TYPED: that looks like several")
    assert browser.skills_problem({"tag": "textarea", "label": "Describe your skills"}, "SQL") is None   # prose answers


def test_a_skill_the_user_confirmed_goes_in(master, monkeypatch):
    monkeypatch.setattr(answers, "saved_answers", lambda: {"Do you have SQL skills?": "Yes, basic"})
    assert browser.skills_problem(BOX, "SQL, User Research") is None
    answers.note_user_message("add Agile too, I have it")
    assert browser.skills_problem(BOX, "Agile") is None


def test_entries_are_split_only_for_skill_and_tag_boxes():
    assert browser.tag_entries(BOX, "Product Strategy, User Research") == ["Product Strategy", "User Research"]
    assert browser.tag_entries(BOX, "User Research") == []
    assert browser.tag_entries({"tag": "input", "type": "text", "label": "Where are you open to working?"}, "A, B") == []
    assert browser.tag_entries({"tag": "textarea", "label": "Skills"}, "A, B") == []


@pytest.fixture()
def ext(monkeypatch):
    from karya import ext_link
    calls, state = [], {"value": "", "picked": ""}

    def request(method, params=None, timeout=45.0):
        p = dict(params or {})
        calls.append((method, p))
        if method == "type_native":
            return {"ok": True, "took": True, "picked": state["picked"]}
        if method == "act" and p.get("op") == "value":
            return {"ok": True, "value": state["value"]}
        return {"ok": True}

    monkeypatch.setattr(ext_link.link, "request", request)
    s = browser.ext_session
    monkeypatch.setattr(s, "items", {5129: dict(BOX)})
    monkeypatch.setattr(s, "url", "https://www.instahyre.com/candidate/onboard/")
    monkeypatch.setattr(s, "_settle", lambda ms=600: None)
    monkeypatch.setattr(s, "_snapshot", lambda **kw: "URL: https://www.instahyre.com/candidate/onboard/")
    return s, calls, state


def test_skills_go_in_one_at_a_time_with_an_enter_that_cant_submit(ext):
    s, calls, _ = ext
    out = s.add_tags(5129, ["Product Strategy", "User Research", "Problem Solving"])
    assert out.startswith('Added 3 entries to "Suggested skills based on your role selected:", one at a time')
    typed = [p["value"] for m, p in calls if m == "type_native"]
    enters = [p["args"] for m, p in calls if m == "act" and p.get("op") == "press"]
    assert typed == ["Product Strategy", "User Research", "Problem Solving"]
    assert len(enters) == 3 and all(a["key"] == "Enter" and a["nosubmit"] is True for a in enters)


def test_a_picked_suggestion_needs_no_enter(ext):
    s, calls, state = ext
    state["picked"] = "Product Strategy"
    s.add_tags(5129, ["Product Strategy", "User Research"])
    assert not [p for m, p in calls if m == "act" and p.get("op") == "press"]


def test_a_plain_text_box_gets_the_whole_list_once(ext):
    s, calls, state = ext
    state["value"] = "Product Strategy"             # the text stayed after Enter: not a tags box
    out = s.add_tags(5129, ["Product Strategy", "User Research"])
    assert "takes the whole list as one text" in out
    assert [p["value"] for m, p in calls if m == "type_native"][-1] == "Product Strategy, User Research"


def test_browser_type_splits_a_skills_list(ext, master, monkeypatch):
    s, calls, _ = ext
    monkeypatch.setattr(browser, "_current", lambda: s)
    monkeypatch.setattr(browser, "_use", lambda: s)
    out = browser.browser_type("Product Strategy, User Research", element_id=5129)
    assert out.startswith("Added 2 entries")


# ---------------------------------------------------------------- page.js: an Enter for tags never submits the form
FORM = """<form id="f" onsubmit="window.sent = (window.sent || 0) + 1; return false;">
<input id="skill"><button type="submit">Next</button></form>"""


def test_tag_enter_never_submits_the_form():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("no Chrome")
        page = b.new_page()
        page.set_content(FORM)
        call = "async (a) => {\n" + browser.PAGE_JS + "\nreturn await window.__karya.act(a.op, a.args);\n}"
        snap = page.evaluate(browser.PAGE_CALL_JS, {"fn": "snapshot", "args": {"next": 1, "reset": True, "max": 50}})
        box = next(i["id"] for i in snap["items"] if i.get("tag") == "input")
        page.evaluate(call, {"op": "press", "args": {"id": box, "key": "Enter", "nosubmit": True}})
        assert page.evaluate("() => window.sent || 0") == 0
        page.evaluate(call, {"op": "press", "args": {"id": box, "key": "Enter"}})      # a normal Enter still does
        assert page.evaluate("() => window.sent || 0") == 1
        b.close()


# ---------------------------------------------------------------- dropdowns whose options have no role (Instahyre)
DROPDOWNS = """<label>When can you join the company if selected?</label>
<div class="dropdown"><button type="button" class="dropdown-toggle" aria-label="Toggle"
  onclick="this.nextElementSibling.style.display='block'">Select</button>
  <ul class="dropdown-menu" style="display:none">
    <li onclick="window.joined=this.textContent; this.parentNode.style.display='none'">Immediately</li>
    <li onclick="window.joined=this.textContent; this.parentNode.style.display='none'">15 days</li>
    <li onclick="window.joined=this.textContent; this.parentNode.style.display='none'">1 month</li></ul></div>
<label>Select your current role:</label>
<div class="ng-select"><div class="ng-select-container" tabindex="0" aria-label="Toggle role"
  onclick="document.getElementById('roles').style.display='block'">Pick a role</div>
  <div id="roles" class="ng-dropdown-panel" style="display:none">
    <div class="ng-option" onclick="window.role=this.textContent; this.parentNode.style.display='none'">Data Engineering</div>
    <div class="ng-option" onclick="window.role=this.textContent; this.parentNode.style.display='none'">Product Management</div>
  </div></div>"""


def test_dropdowns_without_option_roles_can_be_chosen():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            pytest.skip("no Chrome")
        page = b.new_page()
        page.set_content(DROPDOWNS)
        call = "async (a) => {\n" + browser.PAGE_JS + "\nreturn await window.__karya.act(a.op, a.args);\n}"
        snap = page.evaluate(browser.PAGE_CALL_JS, {"fn": "snapshot", "args": {"next": 1, "reset": True, "max": 50}})
        ids = {i["label"]: i["id"] for i in snap["items"]}
        toggle = next(v for k, v in ids.items() if "Toggle" in k and "role" not in k)
        role = next(v for k, v in ids.items() if "role" in k.lower())
        r1 = page.evaluate(call, {"op": "select", "args": {"id": toggle, "option": "Immediately"}})
        r2 = page.evaluate(call, {"op": "select", "args": {"id": role, "option": "Product Management"}})
        assert r1.get("ok") is not False and page.evaluate("() => window.joined") == "Immediately", r1
        assert r2.get("ok") is not False and page.evaluate("() => window.role") == "Product Management", r2
        b.close()
