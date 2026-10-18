"""The fixes from the 2026-10-18 Cisco run: Karya read its own WhatsApp question as the user's answer, saved it,
refused every value, marked Cisco "applied by you", moved to the next job without submitting, and a partial save
wiped the master resume."""
import asyncio
import json
from collections import deque

import pytest

from karya import answers, apply_queue, phone, whatsapp
from karya.tools import autofill, browser, jobs, resume


# ---------------------------------------------------------------- WhatsApp: Karya's own words are never the user's
SENT = ("[Karya] I need your answer:\n1. How many years of work experience do you have? "
        "(Don't include internships - your startup/founder experience counts) (e.g. 1)")
ECHO = ("[Karya] I need your answer:\n\nHow many years of work experience do you have? "
        "(Don't include internships - your startup/founder experience counts) (e.g. 1)")   # as WhatsApp drew it


def test_karyas_question_drawn_as_a_list_is_still_known_as_its_own():
    sent = deque(maxlen=120)
    sent.append(whatsapp._norm(SENT))
    sent.append("plain:" + whatsapp._plain(SENT))
    assert whatsapp._norm(ECHO) != whatsapp._norm(SENT)                 # why it slipped through before
    assert whatsapp.own_message(ECHO, sent)
    assert not whatsapp.own_message("Yes", sent) and not whatsapp.own_message("1", sent)


def _msgs(*items):
    return [{"id": mid, "mine": True, "text": text} for mid, text in items]


def test_new_commands_skip_karyas_own_messages_by_id_and_by_words():
    seen, seen_set, sent = deque(maxlen=400), set(), deque(maxlen=120)
    whatsapp.new_commands(_msgs(("A", "old")), seen, seen_set, sent, primed=False)
    sent.append("plain:" + whatsapp._plain(SENT))
    msgs = _msgs(("A", "old"), ("B", ECHO), ("C", "[Karya] Got it."), ("D", "1"))
    assert whatsapp.new_commands(msgs, seen, seen_set, sent, primed=True, sent_ids={"C"}) == ["1"]


def test_the_bridge_learns_the_ids_of_what_it_sent():
    bridge = whatsapp.WhatsAppBridge("917000000000", lambda text: None)
    bridge._pending_own.append(whatsapp._plain(SENT))
    assert bridge._claim_ids([{"id": "X1", "text": ECHO}, {"id": "X2", "text": "Yes"}])
    assert list(bridge._sent_ids) == ["X1"]


class Bridge:
    state = "ready"

    def __init__(self):
        self.out = []

    def send(self, text):
        self.out.append(text)


def test_an_echoed_question_never_answers_itself():
    from karya.server import Hub

    class Main:
        busy, auto_mode = False, False

    async def go():
        hub = Hub(Main())
        ch = phone.PhoneChannel(hub, None)
        ch.bridge = Bridge()
        future = asyncio.get_running_loop().create_future()
        hub.asks["k"] = (future, {})
        await ch.on_ask({"id": "k", "kind": "questions", "source": "phone",
                         "questions": [{"q": "How many years of work experience do you have?", "options": ["0", "1", "2+"]}]})
        asked = ch.bridge.out[-1]
        assert "1." not in asked and "Choose one: 0 / 1 / 2+" in asked      # no "1." lines WhatsApp turns into lists
        await ch.on_message(ECHO)                                          # Karya's own question read back
        assert not future.done()
        await ch.on_message("1")                                           # the user's real answer
        assert future.result() == {"answers": {"How many years of work experience do you have?": "1"}}
    asyncio.run(go())


# ---------------------------------------------------------------- saved answers
def test_karyas_prompt_is_never_saved_as_the_users_answer():
    q = "How many years of relevant work experience related to this position do you have?"
    answers.save(q, "I need your answer:\n\nHow many years of relevant work experience related to this position do you have?")
    assert answers.saved_answer(q) is None
    answers.save(q, "1")
    assert answers.saved_answer(q) == "1"


@pytest.mark.parametrize("option,saved,ok", [
    ("1-2 years", "1", True), ("1 year", "1", True), ("Less than 1 year", "0", True), ("More than 10 years", "12", True),
    ("0", "fresher", True), ("Less than 1 year", "1", False), ("3-5 years", "1", False), ("1-2 years", "3", False)])
def test_years_options_match_the_users_number(option, saved, ok):
    assert answers._same(option, saved) is ok


def test_years_answer_lets_the_form_option_through():
    q = "How many years of relevant work experience related to this position do you have?"
    answers.save(q, "1")
    assert answers.check(q, "1-2 years") is None
    assert answers.check(q, "3-5 years") is not None


# ---------------------------------------------------------------- "you applied yourself" only when the user says so
@pytest.mark.parametrize("text,marked", [
    ("SPOT DRAFT IS DONE", True), ("I did LoansJagat manually", True), ("applied to Cisco myself", True),
    ("Cisco: applied", True), ("cisco is done, do the next one", True),
    ("did you apply to Cisco?", False), ("Cisco isn't done yet", False), ("apply to the Cisco job", False),
    ("you applied to Cisco, now do CapsLock", False), ("I have not applied to Cisco", False),
    ("continue with cisco, the experience answer is 1 and I already applied for the years thing", False),
    ("finish the cisco application", False)])
def test_done_by_user_needs_a_clear_statement(text, marked):
    apply_queue.start([{"id": "J1", "title": "PM", "company": "Cisco", "url": "u1"},
                       {"id": "J2", "title": "PM", "company": "Spot Draft", "url": "u2"},
                       {"id": "J3", "title": "PM", "company": "LoansJagat", "url": "u3"}])
    assert bool(apply_queue.done_by_user(text)) is marked


# ---------------------------------------------------------------- staying on the current job
def test_karya_finishes_the_current_job_before_opening_another():
    apply_queue.start([{"id": "J67", "title": "EPM", "company": "Cisco", "url": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/Bangalore-India/Engineering-Product-Manager_2021911-1"},
                       {"id": "J68", "title": "APM", "company": "CapsLock", "url": "https://www.instahyre.com/job-443997-associate-product-manager-at-capslock-bangalore/"}])
    apply_queue.set_current({"id": "J67", "url": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/Bangalore-India/Engineering-Product-Manager_2021911-1"})
    stop = browser._open_precheck({"url": "https://www.instahyre.com/job-443997-associate-product-manager-at-capslock-bangalore/"})
    assert stop.startswith("NOT OPENED: you are in the middle of J67") and 'action="skip", job_id="J67"' in stop
    assert browser._open_precheck({"url": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/Bangalore-India/Engineering-Product-Manager_2021911-1/apply"}) is None     # the same job is fine
    apply_queue.mark("J67", "skipped", "closed posting")
    assert browser._open_precheck({"url": "https://www.instahyre.com/job-443997-associate-product-manager-at-capslock-bangalore/"}) is None


def test_a_real_form_problem_can_be_a_skip_reason():
    apply_queue.start([{"id": "J1", "title": "PM", "company": "Cisco", "url": "u1"}])
    assert jobs.application_queue("skip", "J1", "years of experience required").startswith("NOT SKIPPED")
    out = jobs.application_queue("skip", "J1", "the form won't take the years of experience answer; needs your answer")
    assert not str(out).startswith("NOT SKIPPED")


# ---------------------------------------------------------------- the master resume
def test_a_partial_save_never_wipes_the_master_resume():
    full = {"name": "Asha Rao", "experience": [{"title": "PM", "company": "Acme"}], "skills": ["SQL"]}
    resume.save_resume_data(full)
    out = resume.save_resume_data({"years_experience": "1"})
    master = resume.load_master()
    assert master["name"] == "Asha Rao" and master["experience"] and master["years_experience"] == "1"
    assert "experience" in out["kept_as_before"]
    backups = sorted(resume.MASTER_FILE.with_name("resume_backups").glob("resume_master-*.json"))
    assert backups and json.loads(backups[-1].read_text(encoding="utf-8"))["name"] == "Asha Rao"


# ---------------------------------------------------------------- forms
@pytest.mark.parametrize("options,answer,want", [
    (["Select One", "Bachelor of Science", "Bachelor of Technology"], "B.Tech, Computer Science", "Bachelor of Technology"),
    (["Select One", "Bachelor of Science", "Bachelor's Degree"], "B.Tech", "Bachelor's Degree"),
    (["Select One", "Bachelor of Science", "Master of Laws"], "B.Tech", None),        # never a different degree
    (["Andhra Pradesh", "Telangāna", "Karnātaka"], "Telangana", "Telangāna"),          # accents don't count
])
def test_options_for_degrees_and_accents(options, answer, want):
    assert autofill.pick_option(options, answer) == want


def test_approve_and_allow_need_the_users_ok():
    for label in ("Approve", "Allow", "Authorize app", "Grant access"):
        assert browser.classify_click({"tag": "button"}, label, "https://app.example/") == "critical"
    for label in ("Allow all cookies", "Accept all cookies", "Next"):
        assert browser.classify_click({"tag": "button"}, label, "https://app.example/") == "safe"


def test_whatsapp_only_to_numbers_the_user_gave(monkeypatch):
    monkeypatch.setattr(phone, "owner_number", lambda: "917000000000")
    monkeypatch.setattr(answers, "RECENT_USER", ["send the list to my friend at +91 98000 11111"])
    assert browser._compose_precheck({"platform": "whatsapp", "phone": "919876543210", "text": "hi"}).startswith("NOT OPENED")
    assert browser._open_precheck({"url": "https://wa.me/919876543210"}).startswith("NOT OPENED")
    assert browser._compose_precheck({"platform": "whatsapp", "phone": "919800011111", "text": "hi"}) is None
    assert browser._compose_precheck({"platform": "whatsapp", "phone": "917000000000", "text": "hi"}) is None
