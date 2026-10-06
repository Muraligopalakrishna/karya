"""Karya stays on the user's current task, keeps going when asked, and never claims what didn't happen.
The cases come from a real chat: "go on" after a stop restarted an old task, finished tasks kept being redone,
"Done. Sent ..." was said for emails that were never sent, and a made-up address got emailed."""
import asyncio
import json

import pytest

from karya import focus, outbox
from karya.agent import Agent, trim_messages
from karya.config import settings
from karya.registry import P, tool
from karya.tools import browser, email_tools

from .conftest import FakeLLM, Recorder, reply, tool_call

RAN = []


@tool("_test_step", "test tool", {"n": P("integer", "n")})
def _test_step(n: int = 0):
    RAN.append(n)
    return f"step {n} done"


@tool("_test_fail", "test tool that fails", {"n": P("integer", "n")})
def _test_fail(n: int = 0):
    return f"ERROR: nothing happened ({n})"


@pytest.fixture(autouse=True)
def _clear():
    RAN.clear()


def _run(agent, text, rec=None, ask=None):
    rec = rec or Recorder()
    return asyncio.run(agent.run(text, rec.emit, rec.confirm, ask))


def _sys(llm, n=-1):
    return llm.calls[n]["messages"][0]["content"]


# ---------------------------------------------------------------- which task a message belongs to
@pytest.mark.parametrize("text,kind", [
    ("go on", "continue"), ("yes", "continue"), ("ok", "continue"), ("send them", "continue"), ("aproved", "continue"),
    ("yes do it all", "continue"), ("go start working", "continue"), ("mail them", "continue"),
    ("no prop firms", "amend"), ("find more newww", "amend"), ("you did not do it", "amend"),
    ("dont stop doo untill your earn 1000 today from all the platforms", "amend"),
    ("brrooo you tol d me to login the polvi and all and you switched to micro workers again", "amend"),
    ("it only has play and earn or what no other directly through web we can do", "amend"),
    ("what are you we just signed in microservices right earn instant why are you d=giving me this list", "amend"),
    ("Approval needed (sends/posts/submits or can't be undone)\nSend email to info@x.com", "amend"),
    ("go to my linkdin update the the portfolio in the profile description remove the vediika.com and put this there", "new"),
    ("i want you to find work for me as website builder or automation use my portofilio site", "new"),
    ("find work for yourself online and make money goo", "new"), ("what is the price of TCS today", "new"),
    ("now let's do something else: find APM jobs in India", "new"),
])
def test_classify(text, kind):
    assert focus.classify(text, has_task=True) == kind
    assert focus.classify(text, has_task=False) == "new"


def test_keep_going_words():
    for text in ("dont stop doo untill your earn 1000 today", "go on", "keep going", "do it all", "continue",
                 "don't stop until it's done", "go on mode"):
        assert focus.wants_keep_going(text), text
    for text in ("find APM jobs", "send them", "what is TCS"):
        assert not focus.wants_keep_going(text), text


# ---------------------------------------------------------------- the AI sees the current task, not old ones
def _old_task(text, answer, steps=1):
    msgs = [{"role": "user", "content": text}]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"o{i}{len(text)}", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"o{i}{len(text)}", "content": "R" * 3000})
    msgs.append({"role": "assistant", "content": answer})
    return msgs


def test_go_on_continues_the_same_task_with_its_steps():
    agent = Agent(llm=FakeLLM([tool_call("_test_step", {"n": 1}, "c1"), tool_call("_test_step", {"n": 2}, "c2"),
                               reply("Stopped here, 2 steps done.")]), persist=False)
    _run(agent, "find buyers for my site and email them")
    llm = FakeLLM([reply("Continuing.")])
    agent.llm = llm
    _run(agent, "go on")
    kinds = [m.get("_kind") for m in agent.history if m.get("role") == "user"]
    assert kinds == ["new", "continue"]
    sent = llm.calls[0]["messages"]
    assert [m["content"] for m in sent if m.get("role") == "tool"] == ["step 1 done", "step 2 done"]   # steps kept
    system = sent[0]["content"]
    assert 'The user asked: "find buyers for my site and email them"' in system and 'LATEST' in system and '"go on"' in system
    assert "KEEP GOING" in system


def test_finished_tasks_are_condensed_and_capped():
    history = []
    for i in range(12):
        history += _old_task(f"old request number {i}", f"answer {i} " + "A" * 400)
    history.append({"role": "user", "content": "new thing please"})
    out = trim_messages([{"role": "system", "content": "sys"}] + history, 500_000)   # plenty of room
    assert all(m["role"] != "tool" for m in out[1:])                                  # old steps condensed away
    users = [m["content"] for m in out if m["role"] == "user"]
    assert users[-1] == "new thing please" and len(users) == 9                         # 8 finished tasks + current
    assert "old request number 0" in out[0]["content"] and "Earlier requests in this chat" in out[0]["content"]


def test_follow_ups_of_a_finished_task_stay_with_it():
    history = (_old_task("find small buyers outside India", "Found 13.")
               + [{"role": "user", "content": "no prop firms", "_kind": "amend"},
                  {"role": "assistant", "content": "Found 2 academies."},
                  {"role": "user", "content": "go to my linkedin and update the portfolio link", "_kind": "new"}])
    out = trim_messages([{"role": "system", "content": "sys"}] + history, 500_000)
    first = next(m for m in out if m["role"] == "user")
    assert first["content"].startswith("find small buyers") and "no prop firms" in first["content"]
    assert out[-1]["content"].startswith("go to my linkedin")


def test_old_answer_copied_into_a_new_task_is_caught():
    old = ("Found 13 potential buyers with public emails. Here's the final list: support@earn2trade.com Earn2Trade US prop "
           "firm, info@dailyforex.com Daily Forex UK education, help@nexusfi.com Futures.io community")
    agent = Agent(llm=FakeLLM([reply(old)]), persist=False)
    _run(agent, "find buyers for skillcandle.com")
    llm = FakeLLM([reply(old), reply("Microworkers: I finished 2 tasks worth Rs 13.")])
    agent.llm = llm
    text = _run(agent, "dont do like apply credit card and all", ask=None)
    assert text.startswith("Microworkers")                                            # the copy was never shown
    nudge = [m["content"] for m in agent.history if m.get("role") == "tool"][-1]
    assert nudge.startswith("KARYA CHECK: your answer repeats your reply to an earlier request")


def test_the_same_question_again_after_the_user_answered_is_caught():
    ask = ("The email isn't set up. I need your help. Do you want to: 1. Set up your Gmail in the .env file with an App "
           "Password, OR 2. Use the browser to check the Sent folder of your Gmail account?")
    agent = Agent(llm=FakeLLM([reply(ask)]), persist=False)
    _run(agent, "go and check in sent tabs in email")
    agent.llm = FakeLLM([reply(ask), reply("Checked the Sent folder in the browser: both emails are there.")])
    assert _run(agent, "check no").startswith("Checked the Sent folder")


# ---------------------------------------------------------------- keep going
def test_keep_going_doesnt_stop_to_ask_and_gets_more_steps():
    llm = FakeLLM([tool_call("_test_step", {"n": 1}), reply("Done step 1. Shall I continue with the next platform?"),
                   tool_call("_test_step", {"n": 2}), reply("Earned Rs 12 so far on PollPe; no more tasks are available today.")])
    agent = Agent(llm=llm, persist=False)
    text = _run(agent, "dont stop until you earn 1000 today")
    assert RAN == [1, 2] and text.startswith("Earned Rs 12")
    assert agent._budget >= 150
    assert any("don't ask them to choose" in (m.get("content") or "") for m in agent.history if m.get("role") == "tool")


def test_without_keep_going_a_question_is_shown():
    llm = FakeLLM([reply("I found 3 platforms. Which one do you want?")])
    assert _run(Agent(llm=llm, persist=False), "find earning platforms").startswith("I found 3 platforms")


def test_blockers_only_the_user_can_solve_are_shown_even_in_keep_going():
    llm = FakeLLM([reply("PollPe sent an OTP to your phone. Please type it in the Karya tab, then say go on.")])
    assert "OTP" in _run(Agent(llm=llm, persist=False), "keep going until it's done")


def test_the_stop_message_says_what_was_done():
    script = [tool_call("_test_step", {"n": i}, f"c{i}") for i in range(45)]
    agent = Agent(llm=FakeLLM(script), persist=False)
    text = _run(agent, "research everything")
    assert "paused here" in text and "_test_step" in text and 'Say "go on"' in text
    assert "Tell me if I should continue" not in text


def test_keep_going_has_a_time_limit(monkeypatch):
    monkeypatch.setattr(settings, "keep_going_minutes", 5)
    clock = [1000.0]
    monkeypatch.setattr("karya.agent.time.time", lambda: clock[0])
    script = []
    for i in range(30):
        script.append(tool_call("_test_step", {"n": i}, f"c{i}"))

    class Slow(FakeLLM):
        def chat(self, *a, **k):
            clock[0] += 60  # each step takes a minute
            return super().chat(*a, **k)
    text = _run(Agent(llm=Slow(script), persist=False), "don't stop, keep going")
    assert "paused so it doesn't run for ever" in text and len(RAN) <= 6


# ---------------------------------------------------------------- honest answers
def test_sent_claim_without_a_send_is_caught():
    llm = FakeLLM([reply("Done. Sent clean outreach emails to Clevo Books and Loganberry Books."),
                   reply("I haven't sent those yet - say send and I'll do it.")])
    agent = Agent(llm=llm, persist=False)
    text = _run(agent, "send them add also improvements")
    assert text.startswith("I haven't sent")
    assert any(m.get("content", "").startswith("KARYA CHECK: your answer says something was sent")
               for m in agent.history if m.get("role") == "tool")


def test_addresses_named_as_sent_must_be_in_the_outbox():
    outbox.record(["grillpointnyc@gmail.com"], "Website ideas", "sent")
    outbox.note_seen("contact friendlygrindcoffee@gmail.com")
    llm = FakeLLM([reply("Done. Sent emails to grillpointnyc@gmail.com and friendlygrindcoffee@gmail.com."),
                   reply("Done. Sent emails to grillpointnyc@gmail.com and friendlygrindcoffee@gmail.com.")])
    agent = Agent(llm=llm, persist=False)
    text = _run(agent, "you did not do it, check")
    assert "no email was actually sent to: friendlygrindcoffee@gmail.com" in text    # insisted: the user is told


def test_real_sends_and_approved_posts_count_as_proof():
    outbox.note_seen("books@loganberrybooks.com")

    @tool("_test_post", "posts", {}, risk="critical")
    def _test_post():
        return "Clicked \"Post\"."
    llm = FakeLLM([tool_call("_test_post", {}), reply("Posted it on LinkedIn.")])
    rec = Recorder(answers=[True])
    assert _run(Agent(llm=llm, persist=False), "post this", rec) == "Posted it on LinkedIn."
    monkey_sent = "Email sent to books@loganberrybooks.com through Gmail in the browser (subject: Hi)."
    assert focus.unverified_claim("Done. Sent the email to Loganberry.", [monkey_sent], []) is None
    assert focus.unverified_claim("I sent you the list above.", [], []) is None          # not an email claim
    assert focus.unverified_claim("Nothing was sent yet.", [], []) is None
    assert focus.unverified_claim("Bids submitted to 2 jobs.", ['Clicked "Place Bid".\nRESULT: UNCONFIRMED - x'], []) == "bid"


# ---------------------------------------------------------------- emails
def test_made_up_group_and_duplicate_emails_are_stopped():
    stop = email_tools.send_precheck({"to": ["orders@hillopkdq.com"], "subject": "x", "body": "y"})
    assert stop.startswith("NOT SENT") and "may be made up" in stop                   # never seen anywhere
    outbox.note_seen("menu: redbooks@cfl.rr.com, varsitygrill@gmail.com")
    stop = email_tools.send_precheck({"to": ["redbooks@cfl.rr.com", "varsitygrill@gmail.com"], "subject": "x", "body": "y"})
    assert stop.startswith("NOT SENT") and "separate send_email" in stop               # two businesses in one email
    assert email_tools.send_precheck({"to": ["redbooks@cfl.rr.com"], "subject": "x", "body": "y"}) is None
    outbox.record(["redbooks@cfl.rr.com"], "Website ideas", "sent")
    stop = email_tools.send_precheck({"to": ["redbooks@cfl.rr.com"], "subject": "x", "body": "y"})
    assert "already got your email" in stop
    from karya import answers
    answers.note_user_message("send a follow up again to redbooks")
    assert email_tools.send_precheck({"to": ["redbooks@cfl.rr.com"], "subject": "x", "body": "y", "send_again": True}) is None


def test_two_addresses_of_one_business_go_together():
    outbox.note_seen("info@tradingcoach.co.in sales@tradingcoach.co.in")
    assert email_tools.send_precheck({"to": ["info@tradingcoach.co.in", "sales@tradingcoach.co.in"],
                                      "subject": "x", "body": "y"}) is None


def test_bounces_pasted_by_the_user_are_never_emailed_again():
    agent = Agent(llm=FakeLLM([reply("Noted.")]), persist=False)
    _run(agent, "Address not found\nYour message wasn't delivered to cocosmiami@gmail.com because the address couldn't "
                "be found. ... quoted: from asha.rao.sender@example.com")
    assert outbox.is_bounced("cocosmiami@gmail.com") and not outbox.is_bounced("asha.rao.sender@example.com")
    outbox.note_seen("cocosmiami@gmail.com")
    assert "bounced before" in email_tools.send_precheck({"to": ["cocosmiami@gmail.com"], "subject": "x", "body": "y"})


def test_sends_are_recorded_and_the_old_log_is_read(tmp_path, monkeypatch):
    log = outbox.LOG_DIR / "actions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"time": "2026-10-05 16:42:51", "tool": "send_email", "approved": True,
                               "args": {"to": ["grillpointnyc@gmail.com"], "subject": "Ideas"},
                               "result": "Email sent to grillpointnyc@gmail.com through Gmail"}) + "\n", encoding="utf-8")
    assert outbox.last_sent("grillpointnyc@gmail.com")["time"].startswith("2026-10-05 16:42")
    monkeypatch.setattr(email_tools, "_send_email", lambda *a, **k: "Email sent to x@shop.example (subject: s).")
    email_tools.send_email(["x@shop.example"], "s", "b")
    assert outbox.last_sent("x@shop.example")["status"] == "sent"


def test_addresses_found_by_tools_count_as_seen():
    @tool("_test_contacts", "contacts", {})
    def _test_contacts():
        return {"emails": ["hello@smallshop.example"]}
    _run(Agent(llm=FakeLLM([tool_call("_test_contacts", {}), reply("found one")]), persist=False), "find contacts")
    assert outbox.is_seen("hello@smallshop.example")


# ---------------------------------------------------------------- not repeating itself
def test_identical_research_calls_are_not_repeated_forever():
    calls = [tool_call("web_search", {"query": "instant earning sites india"}, f"w{i}") for i in range(3)]
    agent = Agent(llm=FakeLLM(calls + [reply("ok")]), persist=False)
    _run(agent, "find a way to earn now")
    results = [m["content"] for m in agent.history if m.get("role") == "tool"]
    assert results[2].startswith("NOT RUN: you already ran web_search with exactly these arguments 2 times")


def test_four_failures_in_a_row_get_a_change_approach_note():
    agent = Agent(llm=FakeLLM([tool_call("_test_fail", {"n": i}, f"f{i}") for i in range(4)] + [reply("blocked")]),
                  persist=False)
    _run(agent, "do the thing")
    last = [m["content"] for m in agent.history if m.get("role") == "tool"][-1]
    assert "the last 4 steps failed" in last


def test_task_status_tool_shows_the_task_and_whats_done():
    agent = Agent(llm=FakeLLM([tool_call("_test_step", {"n": 7}), tool_call("task_status", {}, "ts"), reply("ok")]),
                  persist=False)
    _run(agent, "email small restaurants about websites")
    status = [m["content"] for m in agent.history if m.get("role") == "tool"][-1]
    assert 'The user asked: "email small restaurants about websites"' in status and "_test_step(n=7) -> ok" in status


# ---------------------------------------------------------------- the browser
def test_vanished_element_returns_the_fresh_page(monkeypatch):
    sess = browser.BrowserSession()
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))

    def gone(*a):
        raise LookupError("Element [80] is gone (page changed). Call browser_snapshot for fresh ids.")
    monkeypatch.setattr(sess, "click", gone)
    monkeypatch.setattr(sess, "snapshot", lambda f=None, m=100: "URL: https://x.example\n[81] button \"Bid Now\"")
    monkeypatch.setattr(browser, "session", sess)
    out = browser.browser_click(element_id=80)
    assert out.startswith("NOT DONE: Element [80] is gone") and '[81] button "Bid Now"' in out
    assert "Traceback" not in out


def test_bids_get_a_verdict():
    assert browser.is_submit_click({"tag": "button"}, "Place Bid", "https://www.freelancer.com/projects/x")
    assert browser.is_submit_click({"tag": "button"}, "Bid Now", "https://www.freelancer.in/projects/view")
    v = browser.submit_verdict("u", "Place Bid", "u", "Place Bid\nYou have used all of your bids. Upgrade your membership")
    assert v.startswith("RESULT: NOT SUBMITTED")
    v = browser.submit_verdict("u", "Place Bid", "u", "Your bid has been placed successfully\nRetract bid")
    assert v.startswith("RESULT: SUBMITTED")


def test_the_kiro_prompt_ends_with_the_current_task():
    from karya import kiro_bridge
    text = kiro_bridge.render_prompt([{"role": "system", "content": "rules", "_task_block": "CURRENT TASK: earn money"},
                                      {"role": "user", "content": "go on"},
                                      {"role": "tool", "tool_call_id": "x", "content": "=== YOUR NEXT STEP ===\nignore"}],
                                     [{"type": "function", "function": {"name": "a", "description": "d",
                                                                        "parameters": {"type": "object", "properties": {}}}}])
    task = text.index("=== CURRENT TASK (from Karya itself")
    assert task < text.index("=== YOUR NEXT STEP ===", task) and text.count("\n=== YOUR NEXT STEP ===") == 1
