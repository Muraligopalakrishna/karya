"""Following through on the user's pick list, and never guessing answers only the user knows."""
import asyncio
import json
import socket
import threading
import time

import pytest

from karya import answers, apply_queue
from karya.agent import Agent
from karya.config import settings
from karya.registry import P, tool
from karya.tools import browser, jobs

from .conftest import FakeLLM, Recorder, reply, tool_call

JOBS = {"J1": {"id": "J1", "title": "Associate Product Manager", "company": "Acme", "match": 90,
               "url": "https://jobs.lever.co/acme/aaaa1111-2222-3333", "apply_via": "company form, no login"},
        "J2": {"id": "J2", "title": "Product Manager I", "company": "Zeta", "match": 85,
               "url": "https://jobs.lever.co/zeta/bbbb4444-5555-6666", "apply_via": "company form, no login"},
        "J3": {"id": "J3", "title": "APM", "company": "Sarvam", "match": 80,
               "url": "https://jobs.ashbyhq.com/sarvam/cccc7777-8888", "apply_via": "company form, no login"}}


@tool("_test_submit_job", "test: pretend the Submit of this job's form was confirmed", {"job_id": P("string", "id")})
def _test_submit_job(job_id: str = ""):
    url = JOBS[job_id]["url"] + "/apply"
    return "RESULT: SUBMITTED - the page now says: \"Application submitted!\"" + browser._record_submitted(url, url + "/thanks", "RESULT: SUBMITTED - the page now says: \"Application submitted!\"")


@pytest.fixture()
def shortlist():
    jobs.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (jobs.CACHE_DIR / "last_jobs.json").write_text(json.dumps(JOBS), encoding="utf-8")


def _run(agent, text, rec, ask):
    return asyncio.run(agent.run(text, rec.emit, rec.confirm, ask))


def _picker(ids, seen=None):
    async def ask(request):
        if seen is not None:
            seen.append(request)
        if request.get("kind") == "jobs":
            return {"picked": ids, "skip_companies": []}
        return None
    return ask


# ---------------------------------------------------------------- answers only the user can give
def test_answers_need_the_users_own_word():
    assert answers.classify("Notice Period ✱") == "notice_period"
    assert answers.classify("Expected CTC") == "expected_salary" and answers.classify("Current CTC ✱") == "current_salary"
    assert answers.classify("Do you have minimum 2 years of work experience with B2B SaaS companies?") == "experience_years"
    assert answers.classify("Do you require visa sponsorship?") == "work_authorization"
    for harmless in ("Current company", "Full name", "Have you built products in the fintech space?", "Manage a team?"):
        assert answers.classify(harmless) is None
    assert "needs the user's own answer" in answers.check("Notice Period ✱", "Immediately")
    assert answers.check("Gender", "Prefer not to disclose") is None      # declining is always truthful
    assert answers.check("Full name", "Asha") is None and answers.check("Notice period", "") is None
    answers.save("Notice Period ✱", "30 days")
    assert answers.check("Notice period", "30 days") is None
    assert answers.check("What is your notice period?", "30 days") is None        # same fact, other wording
    assert 'not "Immediately"' in answers.check("Notice Period", "Immediately")
    answers.save("Do you have at least 2 years of B2B SaaS experience?", "No")
    assert answers.check("Do you have at least 2 years of B2B SaaS experience? *", "No") is None
    assert answers.check("Do you have at least 2 years of B2B SaaS experience?", "Yes") is not None
    assert "needs the user's own answer" in answers.check("How many years of Python experience do you have?", "3")
    # location: the same place or less detail is fine; a city is never guessed from a country-only profile
    answers.save("Location", "India")
    assert answers.check("Current location", "India") is None
    assert "which city is a guess" in answers.check("Location", "Bengaluru, Karnataka, India")
    answers.save("Which city are you in?", "Hyderabad, India")
    assert answers.check("Location", "Hyderabad, Telangana, India") is None and answers.check("City", "Hyderabad") is None
    assert "not \"Pune" in answers.check("Current city", "Pune, Maharashtra, India")
    assert answers.normalize_questions(["Notice period?", {"question": "Gender", "options": ["Male", "Female"]},
                                        "notice period"]) == [{"q": "Notice period?", "options": []},
                                                              {"q": "Gender", "options": ["Male", "Female"]}]


def test_job_dates_are_not_the_notice_period(monkeypatch):
    # 2026-10-06: "Start date year" of a past job was read as "when can you start", so the real date was blocked and
    # the AI saved an invented "March 2026" instead; saving the user's answer would also have replaced their notice period
    from karya.memory import memory_store
    monkeypatch.setattr(jobs, "read_resume", lambda path=None: "Founder - Acme Labs (2023 - Present)\n"
                                                               "Video Editor Intern, Studio (Jul 2024 - Sep 2024)")
    answers.save("Notice period", "Immediately")
    assert answers.classify("Start date year") == answers.classify("End date month") == "history_dates"
    assert answers.classify("When can you start?") == answers.classify("Earliest start date") == "notice_period"
    assert answers.check("Start date year", "2024") is None and answers.check("Start date month", "July") is None
    assert answers.check("End date month", "Present") is None and answers.check("Start date month", "07") is None
    assert "would be a guess" in answers.check("Start date year", "2026")           # not in the resume: invented
    assert "would be a guess" in answers.check("Start date month", "March")
    answers.save("Start date month (Acme Labs)", "March")                            # the user answers (ask_user)
    assert answers.check("Start date month", "March") is None
    assert memory_store.load()["profile"]["notice_period"] == "Immediately"          # untouched


def test_fill_holds_back_guesses(monkeypatch):
    sess = browser.BrowserSession()
    sess.items = {1: {"id": 1, "tag": "input", "type": "text", "label": "Full name ✱"},
                  2: {"id": 2, "tag": "textarea", "label": "Notice Period ✱"},
                  3: {"id": 3, "tag": "select", "label": "Gender ✱", "options": ["Male", "Female", "Prefer not to disclose"]},
                  4: {"id": 4, "tag": "button", "label": "Yes", "question": "Do you have at least 2 years of B2B SaaS experience?"}}
    filled = []
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "fill_many", lambda fields: filled.append(dict(fields)) or "Filled.")
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    out = browser.browser_fill({"1": "Asha Rao", "2": "Immediately", "3": "Male", "4": "Yes"})
    assert out.startswith("NOT FILLED") and "Notice Period" in out and "Gender" in out and "B2B SaaS" in out
    assert filled == [{"1": "Asha Rao"}]                                        # only the safe field went in
    answers.save("Notice Period", "30 days")
    assert browser.browser_fill({"2": "30 days", "3": "Prefer not to disclose"}) == "Filled."
    assert browser._select_precheck({"element_id": 3, "option": "Female"}).startswith("NOT SELECTED")
    assert browser._click_precheck({"element_id": 4}).startswith("NOT CLICKED")


# ---------------------------------------------------------------- the pick list is followed through
def test_posting_keys_and_queue():
    assert apply_queue.posting_key("https://jobs.lever.co/zeta/8623c195-f912-4d87-952f-7114cd258413") == \
        "8623c195-f912-4d87-952f-7114cd258413"
    assert apply_queue.posting_key("https://www.linkedin.com/jobs/search/?currentJobId=4012345678&f=1") == "4012345678"
    assert apply_queue.posting_key("https://boards.greenhouse.io/acme/jobs/7654321") == "7654321"
    apply_queue.start([JOBS["J1"], JOBS["J2"]])
    assert [j["id"] for j in apply_queue.pending()] == ["J1", "J2"]
    assert apply_queue.find_by_url("https://jobs.lever.co/acme/aaaa1111-2222-3333/thanks")["id"] == "J1"
    apply_queue.mark("J1", "applied", "Application submitted!")
    nudge = apply_queue.nudge_text()
    assert "J2" in nudge and "don't ask" in nudge.lower()
    summary = apply_queue.summary_for_user()
    assert "\u2714 Acme" in summary and "\u2026 Zeta" in summary
    assert apply_queue.wanted_in("apply to the rest") and apply_queue.wanted_in("continue the jobs")
    assert not apply_queue.wanted_in("continue")                    # the last task wasn't the job list
    apply_queue.note_run(True)
    assert apply_queue.wanted_in("continue") and apply_queue.wanted_in("SPOT DRAFT IS DONE DO NEXTY")
    assert not apply_queue.wanted_in("stop applying") and not apply_queue.wanted_in("what's the nifty today")
    assert not apply_queue.wanted_in("I WANT YOU TO FIND POTENTIAL BUYER FOR SKILLCANDLE.COM AND SEND EMAILS AND DO IT")
    assert not apply_queue.wanted_in("i told to mail and started the jobs i dont know y you started it")


def test_agent_applies_to_every_picked_job(shortlist):
    llm = FakeLLM([tool_call("choose_jobs", {"job_ids": ["J2"]}, "c1"),
                   tool_call("_test_submit_job", {"job_id": "J1"}, "c2"),
                   reply("Applied to Acme! Would you like me to apply to more roles?"),   # stops too early
                   tool_call("_test_submit_job", {"job_id": "J2"}, "c3"),
                   reply("All done.")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    text = _run(agent, "find APM jobs and apply", rec, _picker(["J1", "J2"]))
    nudges = [m for m in agent.history if m.get("role") == "tool" and m["content"].startswith("NOT FINISHED")]
    assert len(nudges) == 1 and "Zeta" in nudges[0]["content"]
    assert [j["status"] for j in apply_queue.load()] == ["applied", "applied"]
    assert text.startswith("All done.") and text.count("\u2714") == 2 and "submitted (the site confirmed it)" in text
    tracked = jobs.list_applications()
    assert [(a["company"], a["status"]) for a in tracked] == [("Acme", "applied"), ("Zeta", "applied")]
    assert agent._budget > 40                                                   # room for several applications


def test_agent_can_still_stop_when_stuck(shortlist):
    llm = FakeLLM([tool_call("choose_jobs", {}, "c1"), reply("I need your Workday login."), reply("Still stuck.")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    text = _run(agent, "apply to APM jobs", rec, _picker(["J1", "J3"]))
    assert text.startswith("Still stuck.") and "\u2026 Acme" in text and "not done yet" in text
    assert len(llm.calls) == 3                                                  # one nudge, then it may stop


def test_a_later_continue_picks_up_the_rest(shortlist):
    apply_queue.start([JOBS["J1"], JOBS["J2"]])
    apply_queue.mark("J1", "applied", "ok")
    apply_queue.note_run(True)                                         # the last task was this job list
    llm = FakeLLM([reply("Done for now.")])
    agent = Agent(llm=llm, persist=False)
    _run(agent, "continue", Recorder(), _picker([]))
    assert "J2 Product Manager I at Zeta" in llm.calls[0]["messages"][0]["content"]   # the prompt lists what's left
    llm2 = FakeLLM([reply("Nifty is up.")])
    Agent(llm=llm2, persist=False) and asyncio.run(Agent(llm=llm2, persist=False).run("what's the nifty today", Recorder().emit,
                                                                                       Recorder().confirm, _picker([])))
    assert "Picked jobs still to apply" not in llm2.calls[0]["messages"][0]["content"]   # other tasks aren't steered


def test_denied_submit_skips_that_job_and_the_card_lists_the_answers(shortlist, monkeypatch):
    apply_queue.start([JOBS["J1"], JOBS["J2"]])
    sess = browser.BrowserSession()
    sess.items = {5: {"id": 5, "tag": "button", "label": "Submit application"}}
    sess.url = JOBS["J1"]["url"] + "/apply"
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "form_check", lambda eid: {"empty": []})
    monkeypatch.setattr(sess, "form_values", lambda eid: [{"q": "Full name", "v": "Asha Rao"}, {"q": "Notice period", "v": "30 days"}])
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    llm = FakeLLM([tool_call("browser_click", {"element_id": 5}), reply("Okay.")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[False])
    _run(agent, "apply to the rest", rec, _picker([]))
    card = rec.requests[0]["summary"]
    assert card.startswith("Submit your application: Associate Product Manager at Acme")
    assert "What this form will send" in card and "- Notice period: 30 days" in card
    denied = [m for m in agent.history if m.get("role") == "tool"][0]["content"]
    assert denied.startswith("The user DENIED this Submit") and "J1 (Acme) as skipped" in denied and "Zeta" in denied
    assert apply_queue.load()[0]["status"] == "skipped"


def test_only_picked_jobs_are_submitted(shortlist, monkeypatch):
    apply_queue.start([JOBS["J1"]])                      # the user picked only J1
    sess = browser.BrowserSession()
    sess.items = {5: {"id": 5, "tag": "button", "label": "Submit application"},
                  6: {"id": 6, "tag": "input", "type": "tel", "label": "Phone ✱"}}
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "form_check", lambda eid: {"empty": []})
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    sess.url = JOBS["J2"]["url"] + "/apply"             # Zeta: on the shortlist but not picked
    for args in ({"element_id": 5}, {"element_id": 5, "confirm_empty": True}):
        stop = browser._click_precheck(args)
        assert stop.startswith("NOT CLICKED") and "J2" in stop and "not on the list" in stop and "choose_jobs" in stop
    sess.url = JOBS["J1"]["url"] + "/apply"
    assert browser._click_precheck({"element_id": 5}) is None
    sess.url = "https://jobs.lever.co/other/9999aaaa-1111/apply"   # a link the user gave: their own request
    assert browser._click_precheck({"element_id": 5}) is None
    stop = browser._type_precheck({"element_id": 6, "text": "+91 70000 00000", "submit": True})
    assert stop.startswith("NOT TYPED") and "Enter" in stop


def test_ask_user_card_saves_answers():
    seen = []

    async def ask(request):
        seen.append(request)
        return {"answers": {"Notice period?": "30 days", "Gender": "Male"}}
    llm = FakeLLM([tool_call("ask_user", {"questions": ["Notice period?", {"question": "Gender", "options": ["Male", "Female"]}],
                                         "reason": "Acme asks"}), reply("ok")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    _run(agent, "apply", rec, ask)
    assert seen[0]["kind"] == "questions" and seen[0]["questions"][1]["options"] == ["Male", "Female"]
    result = json.loads([m for m in agent.history if m.get("role") == "tool"][0]["content"])
    assert result["answers"] == {"Notice period?": "30 days", "Gender": "Male"}
    assert answers.saved_answer("What's your notice period") == "30 days" and answers.saved_answer("Gender ✱") == "Male"
    llm2 = FakeLLM([tool_call("ask_user", {"questions": ["Expected CTC?"]}), reply("ok")])
    agent2 = Agent(llm=llm2, persist=False)
    _run(agent2, "apply", Recorder(), _picker([]))                                  # the user skipped the card
    assert "didn't answer" in [m for m in agent2.history if m.get("role") == "tool"][0]["content"]


def test_new_search_never_reuses_picked_ids(shortlist):
    apply_queue.start([JOBS["J1"], JOBS["J2"]])
    (jobs.CACHE_DIR / "last_jobs.json").unlink()            # e.g. the shortlist expired, then a new search
    rows = [{"title": "APM", "company": "Navi", "url": "https://www.linkedin.com/jobs/view/4472516899/"},
            {"title": "APM", "company": "Zeta", "url": JOBS["J2"]["url"]}]          # already picked: keeps its id
    jobs.remember_jobs(rows, [])
    assert [r["id"] for r in rows] == ["J3", "J2"]
    assert jobs.job_by_id("J1")["company"] == "Acme"        # a picked job is still found by its id
    assert jobs.job_by_id("J3")["company"] == "Navi"


def test_submit_of_a_shortlist_job_never_mislabels_the_pick_list(shortlist):
    apply_queue.start([{**JOBS["J1"], "id": "J1"}])
    other = {"J1": {**JOBS["J3"], "id": "J1"}}               # a newer shortlist reused the id J1 for another job
    (jobs.CACHE_DIR / "last_jobs.json").write_text(json.dumps(other), encoding="utf-8")
    url = JOBS["J3"]["url"] + "/application"
    note = browser._record_submitted(url, url, 'RESULT: SUBMITTED - the page now says: "Thanks"')
    assert "saved this in the tracker" in note and jobs.list_applications()[0]["company"] == "Sarvam"
    assert apply_queue.load()[0]["status"] == "pending"      # Acme (J1 on the pick list) is NOT marked applied


def test_pick_list_pre_ticks_what_was_proposed(shortlist):
    seen = []
    llm = FakeLLM([tool_call("choose_jobs", {"job_ids": ["J1", "J2", "J3"]}), reply("ok")])
    _run(Agent(llm=llm, persist=False), "apply to all of them", Recorder(), _picker(["J1"], seen))
    card = seen[0]
    assert [j["suggested"] for j in card["jobs"]] == [True, True, True]
    seen.clear()
    llm = FakeLLM([tool_call("choose_jobs", {"job_ids": ["J2"]}), reply("ok")])
    _run(Agent(llm=llm, persist=False), "find jobs", Recorder(), _picker(["J2"], seen))
    assert {j["id"]: j["suggested"] for j in seen[0]["jobs"]} == {"J1": False, "J2": True, "J3": False}


def test_visa_rule_from_the_user_covers_visa_questions():
    assert "needs the user's own answer" in answers.check("Will you require visa sponsorship?", "No")
    answers.save("Do you need visa sponsorship?", "No for jobs in India; yes for other countries")
    assert answers.check("Will you now or in the future require sponsorship?", "No") is None
    assert answers.check("Are you authorized to work in the US?", "No") is None


def test_captchas_are_left_to_the_user_and_careers_send_is_a_submit(monkeypatch):
    sess = browser.BrowserSession()
    sess.items = {1: {"id": 1, "tag": "input", "type": "checkbox", "label": "I'm not a robot"},
                  2: {"id": 2, "tag": "button", "label": "Send"}, 3: {"id": 3, "tag": "input", "label": "Name"}}
    sess.url = "https://www.swish.global/careers/"
    filled = []
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "fill_many", lambda f: filled.append(dict(f)) or "Filled.")
    monkeypatch.setattr(sess, "form_check", lambda eid: {"empty": ["Interested Department"]})
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    assert "CAPTCHA" in browser._check_precheck({"element_id": 1}) and "CAPTCHA" in browser._click_precheck({"element_id": 1})
    out = browser.browser_fill({"1": "true", "3": "Asha"})
    assert "CAPTCHA" in out and filled == [{"3": "Asha"}]
    assert browser.is_submit_click(sess.items[2], "Send", sess.url)                     # a careers form
    assert not browser.is_submit_click(sess.items[2], "Send", "https://www.linkedin.com/messaging/")
    assert browser._click_precheck({"element_id": 2}).startswith("NOT CLICKED")       # required field still empty


def test_guard_reads_the_label_when_the_question_is_a_counter():
    item = {"id": 94, "tag": "input", "type": "text", "label": "What is your current CTC?", "question": "0/20 0 of 20 characters"}
    assert "needs the user's own answer" in browser.answer_problem(item, "3.5 LPA")


def test_answers_from_the_chat_count_when_on_topic(monkeypatch):
    monkeypatch.setattr(answers, "RECENT_USER", [])
    answers.note_user_message("apply to 30 jobs please")
    assert "needs the user's own answer" in answers.check("Notice period", "30 days")      # 30 wasn't about notice
    answers.note_user_message("my notice period is 30 days and current CTC is 0, expected 6 LPA. I live in Hyderabad")
    assert answers.check("Notice period", "30 days") is None and answers.saved_answer("Notice Period") == "30 days"
    assert answers.check("Expected CTC", "6 LPA") is None
    assert 'not "8 LPA"' in answers.check("Expected CTC", "8 LPA")                       # not what the user said
    assert answers.check("Current location", "Hyderabad") is None
    assert "needs the user's own answer" in answers.check("Do you have 2+ years of B2B SaaS experience?", "No")


def test_profile_facts_come_from_the_users_own_words(monkeypatch):
    from karya.tools.memory_tools import update_profile
    monkeypatch.setattr(answers, "RECENT_USER", [])
    assert update_profile("notice_period", "30 days").startswith("ERROR")          # a guess by the AI
    assert update_profile("current_salary", "3.5 LPA").startswith("ERROR")
    answers.note_user_message("IN INDIA NO VISA SPONSORSHIP, FOR OTHER COUNTRIES YES. notice period is 30 days")
    assert update_profile("notice_period", "30 days") == "Profile updated: notice_period"
    assert update_profile("work_authorization", "No sponsorship needed in India; yes for other countries").startswith("Profile")
    assert update_profile("headline", "Founder").startswith("Profile")                # ordinary fields are untouched


def test_tracker_updates_instead_of_duplicating():
    jobs.track_application("Zeta", "Product Manager I", "https://x/1", "applied")
    out = jobs.track_application("zeta", "product manager i", "", "interview", notes="call on Monday")
    assert out.startswith("Updated application #1") and len(jobs.list_applications()) == 1


# ---------------------------------------------------------------- questions card through the real server
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_questions_card_over_the_websocket():
    import uvicorn
    import websockets

    from karya.server import create_app
    port, token = _free_port(), "ws-token-0123456789abcdefghij"
    llm = FakeLLM([tool_call("ask_user", {"questions": ["Notice period?"], "reason": "Acme asks"}), reply("Thanks!")])
    app = create_app(agent=Agent(llm=llm, persist=False), token=token, port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    got = {}

    async def scenario():
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={token}", origin=f"http://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({"type": "chat", "text": "apply"}))
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if ev["type"] == "ask":
                    got["card"] = ev
                    await ws.send(json.dumps({"type": "ask_reply", "id": ev["id"], "answers": {"Notice period?": "30 days"}}))
                if ev["type"] == "assistant":
                    got["final"] = ev["text"]
                    return

    errors = []
    thread = threading.Thread(target=lambda: _safe(asyncio.run, scenario(), errors))
    thread.start()
    thread.join(20)
    server.should_exit = True
    if errors:
        raise errors[0]
    assert got["card"]["kind"] == "questions" and got["card"]["questions"][0]["q"] == "Notice period?"
    assert got["final"] == "Thanks!" and answers.saved_answer("Notice period") == "30 days"


def _safe(fn, arg, errors):
    try:
        fn(arg)
    except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
        errors.append(exc)
