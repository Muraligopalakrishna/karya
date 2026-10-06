"""Speed and "full access" for the user's picked jobs, without repeated questions or logins."""
import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from karya import answers, apply_queue, vault
from karya.agent import Agent
from karya.config import settings
from karya.tools import browser, jobs, resume

from .conftest import FakeLLM, Recorder, reply, tool_call

NAVI = {"id": "J12", "title": "Associate Product Manager", "company": "Navi",
        "url": "https://in.linkedin.com/jobs/view/associate-product-manager-at-navi-4472516899", "apply_via": "LinkedIn"}
ACME = {"id": "J1", "title": "APM", "company": "Acme", "url": "https://jobs.lever.co/acme/8623c195-f912-4d87-952f-7114cd258413"}


def _run(agent, text, rec, ask=None):
    return asyncio.run(agent.run(text, rec.emit, rec.confirm, ask))


def _session(monkeypatch, url, items):
    sess = browser.BrowserSession()
    sess.items, sess.url = items, url
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "form_check", lambda eid: {"empty": []})
    monkeypatch.setattr(sess, "form_values", lambda eid: [{"q": "Phone", "v": "+91 70000 00000"}])
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    return sess


def test_linkedin_easy_apply_page_matches_the_picked_job():
    assert apply_queue.posting_key(NAVI["url"]) == "4472516899"
    apply_queue.start([NAVI, ACME])
    assert apply_queue.find_by_url("https://www.linkedin.com/jobs/view/4472516899/?companyName=Navi&trk=x")["id"] == "J12"
    assert apply_queue.find_by_url(ACME["url"] + "/apply")["id"] == "J1"
    assert apply_queue.find_by_url("https://www.linkedin.com/jobs/view/4999999999/") is None


def test_pre_approved_submits_are_limited_and_never_repeat(monkeypatch):
    sess = _session(monkeypatch, "https://www.linkedin.com/jobs/view/4472516899/",
                    {7: {"id": 7, "tag": "button", "label": "Submit application"}, 8: {"id": 8, "tag": "button", "label": "Next"}})
    apply_queue.start([NAVI], auto_submit=False)
    assert browser.pre_approved_submit({"element_id": 7}) is None             # not pre-approved: the card is shown
    apply_queue.set_auto_submit(True)
    assert browser.pre_approved_submit({"element_id": 8}) is None             # "Next" isn't a Submit
    note = browser.pre_approved_submit({"element_id": 7})
    assert note.startswith("Submitting Associate Product Manager at Navi without asking") and "Phone" in note
    apply_queue.note_result(sess.url, "unconfirmed")
    assert browser.pre_approved_submit({"element_id": 7}) is None             # unclear result: ask the user next time
    apply_queue.note_result(sess.url, "not_submitted")
    assert browser.pre_approved_submit({"element_id": 7}) is not None         # 2nd try allowed
    assert browser.pre_approved_submit({"element_id": 7}) is None             # never more than 2
    apply_queue.mark("J12", "applied", "Your application was sent")
    assert "already applied" in browser._click_precheck({"element_id": 7})    # never submitted twice


def test_agent_submits_picked_jobs_without_a_card_when_pre_approved(monkeypatch):
    sess = _session(monkeypatch, "https://www.linkedin.com/jobs/view/4472516899/",
                    {7: {"id": 7, "tag": "button", "label": "Submit application"}})
    monkeypatch.setattr(sess, "click", lambda *a: 'Clicked "Submit application".\nRESULT: SUBMITTED - the page now says: '
                                                  '"Your application was sent to Navi!"')
    apply_queue.start([NAVI], auto_submit=True)
    for q, v in (("What is your notice period?", "Immediately"), ("What is your current CTC (salary per year)?", "0"),
                 ("What is your expected CTC (salary per year)?", "6 LPA"), ("Which city do you live in?", "Hyderabad"),
                 ("Are you willing to relocate for a job (e.g. to Bangalore)?", "Yes"), ("Gender", "Male")):
        answers.save(q, v)
    llm = FakeLLM([tool_call("browser_click", {"element_id": 7}), reply("Done.")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    text = _run(agent, "apply to the rest of my jobs", rec)
    assert rec.requests == []                                                 # no approval card
    assert any(e["type"] == "note" and "without asking" in e["text"] for e in rec.events)
    assert apply_queue.load()[0]["status"] == "applied" and "\u2714 Navi" in text
    assert [a["company"] for a in jobs.list_applications()] == ["Navi"]


def test_saved_login_is_never_asked_again():
    vault.save_account("linkedin.com", "asha@example.org", "s3cret-pass")
    asked = []

    async def ask(request):
        asked.append(request)
        return {"username": "asha@example.org", "password": "new-pass"}
    llm = FakeLLM([tool_call("request_credentials", {"site": "www.linkedin.com"}, "c1"),
                   tool_call("request_credentials", {"site": "linkedin.com", "wrong_password": True}, "c2"), reply("ok")])
    agent = Agent(llm=llm, persist=False)
    _run(agent, "apply on linkedin", Recorder(), ask)
    results = [m["content"] for m in agent.history if m.get("role") == "tool"]
    assert "already" in results[0] and "don't ask the user again" in results[0]
    assert len(asked) == 1 and "didn't work" in asked[0]["reason"]           # asked only for a rejected password
    assert vault.get_secret("linkedin.com") == ("asha@example.org", "new-pass")


def test_basics_are_asked_once_and_reused():
    apply_queue.start([NAVI, ACME])
    seen = []

    async def ask(request):
        seen.append(request)
        return {"answers": {"What is your notice period?": "Immediately", "Which city do you live in?": "Hyderabad",
                            "What is your expected CTC (salary per year)?": "6 LPA"}}
    _run(Agent(llm=FakeLLM([reply("working")]), persist=False), "continue applying", Recorder(), ask)
    assert len(seen) == 1 and seen[0]["kind"] == "questions"
    assert {q["q"] for q in seen[0]["questions"]} >= {"What is your notice period?", "Gender", "Which city do you live in?"}
    assert answers.saved_answer("Notice Period ✱") == "Immediately" and answers.saved_answer("Current location") == "Hyderabad"
    assert answers.check("Expected CTC", "6 LPA") is None
    seen.clear()
    _run(Agent(llm=FakeLLM([reply("working")]), persist=False), "continue applying", Recorder(), ask)
    left = {q["q"] for q in seen[0]["questions"]} if seen else set()
    assert "What is your notice period?" not in left and "Which city do you live in?" not in left   # never again
    seen.clear()
    _run(Agent(llm=FakeLLM([reply("Nifty is up")]), persist=False), "how is the nifty today", Recorder(), ask)
    assert seen == []                                                          # other tasks don't get the card


def test_a_country_alone_doesnt_count_as_the_city():
    answers.save("Location", "India")
    assert "Which city do you live in?" in {q["q"] for q in answers.missing_basics()}


def test_answers_from_chat_survive_a_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(answers, "RECENT_USER", [])
    from karya import agent as agent_mod
    history = [{"role": "user", "content": "0 NOTICE PERIOD IMMEDIATE, 2 YEARS building my products"},
               {"role": "assistant", "content": "ok"}]
    agent_mod.HISTORY_FILE.write_text(json.dumps(history), encoding="utf-8")
    Agent(llm=FakeLLM([]), persist=True)
    assert answers.check("How many years of product experience do you have?", "2") is None
    assert "needs the user's own answer" in answers.check("Do you have 2+ years of B2B SaaS experience?", "Yes")


def test_search_is_skipped_while_working_through_picked_jobs(monkeypatch):
    apply_queue.start([NAVI])
    monkeypatch.setattr(answers, "RECENT_USER", ["continue"])
    stop = jobs._search_precheck()
    assert stop.startswith("NOT RUN") and "Navi" in stop
    monkeypatch.setattr(answers, "RECENT_USER", ["find more APM jobs in Pune"])
    assert jobs._search_precheck() is None


def test_job_details_are_remembered(monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "_job_details", lambda url, job_id="": calls.append(url) or {"title": "APM", "description": "x"})
    monkeypatch.setattr(jobs, "_DETAILS_CACHE", {})
    first = jobs.get_job_details(url="https://jobs.lever.co/acme/1234567")
    second = jobs.get_job_details(url="https://jobs.lever.co/acme/1234567")
    assert first == second and len(calls) == 1


def test_resumes_for_the_next_jobs_are_made_in_the_background(monkeypatch, tmp_path):
    from karya import llm
    resume.MASTER_FILE.write_text(json.dumps({"name": "Asha", "skills": ["SQL"]}), encoding="utf-8")
    apply_queue.start([NAVI, ACME])
    made, lanes = [], []

    def fake_tailor(job_url="", job_title="", company="", job_description="", job_id=""):
        lanes.append(getattr(llm.LANE, "name", "main"))
        made.append(job_id)
        pdf = tmp_path / f"{job_id}.pdf"
        pdf.write_bytes(b"%PDF")
        return {"pdf": str(pdf)}
    monkeypatch.setattr(resume, "_tailor", fake_tailor)
    monkeypatch.setattr(resume, "_can_prepare", lambda: True)
    monkeypatch.setattr(resume, "_PREPARED", {})
    resume.prepare_next()
    for _ in range(50):
        if len(made) == 2:
            break
        time.sleep(0.05)
    assert sorted(made) == ["J1", "J12"] and set(lanes) == {"bg"}             # its own Kiro process
    out = resume.tailor_resume(job_id="J12")
    assert out["pdf"].endswith("J12.pdf") and made.count("J12") == 1          # used, not made again


def test_background_requests_dont_share_the_agents_prompt():
    from karya.llm import LLMClient
    client = LLMClient([])
    client._system_for = "agent"
    seen = []
    worker = threading.Thread(target=lambda: seen.append(client._system_for))
    worker.start()
    worker.join()
    assert seen == [None] and client._system_for == "agent"


# ---------------------------------------------------------------- the chat page after an update
ORIGIN = {"origin": "http://testserver"}
TOKEN = "tok-0123456789012345678901"


def _client(script=()):
    from fastapi.testclient import TestClient

    from karya.server import create_app
    agent = Agent(llm=FakeLLM(list(script)), persist=False)
    app = create_app(agent=agent, token=TOKEN, port=8765, extra_hosts={"testserver"})
    return TestClient(app), agent


def test_page_knows_its_version_and_reloads_when_stale():
    from karya.server import ui_version
    client, _ = _client()
    page = client.get("/").text
    version = ui_version()
    assert f'<meta name="karya-ui" content="{version}">' in page and f"/static/app.js?v={version}" in page
    with client.websocket_connect(f"/ws?token={TOKEN}&ui=old123", headers=ORIGIN) as ws:
        assert ws.receive_json()["type"] == "history" and ws.receive_json() == {"type": "reload"}
    with client.websocket_connect(f"/ws?token={TOKEN}&ui={version}", headers=ORIGIN) as ws:
        assert ws.receive_json()["type"] == "history"
    with client.websocket_connect(f"/ws?token={TOKEN}", headers={**ORIGIN, "user-agent": "Mozilla/5.0 Chrome/154"}) as ws:
        ws.receive_json()
        notice = ws.receive_json()
        assert notice["type"] == "error" and "F5" in notice["text"]


def test_old_login_style_reply_to_a_question_is_dropped():
    from karya.server import ui_version
    client, agent = _client([tool_call("ask_user", {"questions": ["Notice period?"]}), reply("Thanks!")])
    with client.websocket_connect(f"/ws?token={TOKEN}&ui={ui_version()}", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_json({"type": "chat", "text": "apply"})
        card = None
        while card is None:
            ev = ws.receive_json()
            if ev["type"] == "ask":
                card = ev
        assert card["kind"] == "questions"
        # what an old page sent: its "login" form fields
        ws.send_json({"type": "ask_reply", "id": card["id"], "username": "me@example.org", "password": "my-password"})
        warning = ws.receive_json()
        assert warning["type"] == "error" and "thrown away" in warning["text"]
        ws.send_json({"type": "ask_reply", "id": card["id"], "answers": {"Notice period?": "Immediately"}})
        final = None
        while final is None:
            ev = ws.receive_json()
            if ev["type"] == "assistant":
                final = ev["text"]
    assert final == "Thanks!" and answers.saved_answer("Notice period") == "Immediately"
    result = [m["content"] for m in agent.history if m.get("role") == "tool"][0]
    assert "my-password" not in json.dumps(agent.history) and "Immediately" in result
