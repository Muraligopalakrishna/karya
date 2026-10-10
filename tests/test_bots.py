"""Bots (karya/scheduler.py + the Hub): named helpers that work in parallel with the chat, take turns on the
browser (karya/desk.py), keep notes, and are reachable with "@Name" in the chat and on WhatsApp."""
import asyncio
import time

import pytest

from karya import desk, phone, runctx, scheduler
from karya.agent import Agent
from karya.server import Hub

from .conftest import FakeLLM, reply, tool_call


class MainAgent:
    """The chat's agent: finishes after a short while."""

    def __init__(self):
        self.busy, self.cancelled, self.seen, self.auto_mode = False, False, [], False

    async def run(self, text, emit, confirm, ask, user_words=None):
        self.busy = True
        self.seen.append(text)
        await asyncio.sleep(0.05)
        self.busy = False
        return "chat answer"

    def cancel(self):
        self.cancelled = True


class BotAgent:
    """A bot's agent: optionally asks for an approval, then reports."""

    def __init__(self, report="Found 3 PM jobs.\nRemember: covered Swiggy and Zepto", ask_first=False, wait=0.05):
        self.report, self.ask_first, self.wait = report, ask_first, wait
        self.busy, self.cancelled, self.runs, self.answers = False, False, [], []
        self.bot, self.auto_mode = None, False

    async def run(self, text, emit, confirm, ask, user_words=None):
        self.busy = True
        self.runs.append({"text": text, "user_words": user_words, "run": runctx.current()})
        await emit({"type": "tool_call", "id": "c1", "name": "web_search", "summary": "search"})
        await emit({"type": "busy", "value": True})                      # never reaches the chat
        if self.ask_first:
            self.answers.append(await confirm({"id": "x", "summary": "Send the email to HR"}))
        await asyncio.sleep(self.wait)
        self.busy = False
        return "Stopped." if self.cancelled else self.report

    def cancel(self):
        self.cancelled = True


def _hub(bot_factory=None):
    hub = Hub(MainAgent())
    events = []

    async def capture(event):
        events.append(event)
    hub.send = capture
    made = {}

    def make(record):
        made[record["name"]] = (bot_factory or BotAgent)()
        return made[record["name"]]
    hub.make_bot = make
    return hub, events, made


async def _settle(hub, timeout=3.0):
    end = time.time() + timeout
    while (hub.bots or hub.bot_queue or hub.busy_now()) and time.time() < end:
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- bots
def test_addressed():
    scheduler.create("Maya", "Find PM jobs")
    scheduler.create("Job hunter", "Apply to jobs")
    assert scheduler.addressed("@Maya find 3 PM jobs in Dubai")[1] == "find 3 PM jobs in Dubai"
    assert scheduler.addressed("maya: check gold")[0]["name"] == "Maya"
    assert scheduler.addressed("@job hunter, apply to two more")[1] == "apply to two more"
    assert scheduler.addressed("@Mayank hello") is None                    # another name
    assert scheduler.addressed("Maya find jobs") is None                   # plain text stays a chat message
    assert scheduler.addressed("@Maya") is None                            # no task


def test_notes_from_remember_lines():
    bot = scheduler.create("Maya", "Find PM jobs")
    assert scheduler.remember(bot["id"], "Done.\nRemember: applied to Swiggy\n- Remember: Zepto wants a referral") == \
        ["applied to Swiggy", "Zepto wants a referral"]
    scheduler.remember(bot["id"], "Remember: applied to Swiggy")             # no duplicates
    assert scheduler.find("maya")["notes"] == ["applied to Swiggy", "Zepto wants a referral"]
    for n in range(30):
        scheduler.remember(bot["id"], f"Remember: note {n}")
    assert len(scheduler.find("maya")["notes"]) == scheduler.MAX_NOTES


def test_a_bot_works_while_the_chat_stays_free():
    async def go():
        hub, events, made = _hub()
        hub.listeners.append(scheduler.Hooks())
        maya = scheduler.create("Maya", "Find PM jobs in India and apply")
        assert await hub.assign(maya["id"], "find 3 PM jobs in Dubai", user_words="find 3 PM jobs in Dubai") == "started"
        assert hub.bot_running(maya["id"]) and not hub.busy_now()           # the chat is still free
        assert await hub.submit("what's the weather?") == "started"          # and takes a task meanwhile
        await _settle(hub)
        run = made["Maya"].runs[0]
        assert 'You are "Maya"' in run["text"] and "find 3 PM jobs in Dubai" in run["text"]
        assert run["user_words"] == "find 3 PM jobs in Dubai" and run["run"].bot == "Maya" and run["run"].is_bot
        assert hub.agent.seen == ["what's the weather?"]
        kinds = [(e["type"], e.get("bot")) for e in events]
        assert ("bot_started", "Maya") in kinds and ("bot_done", "Maya") in kinds and ("tool_call", "Maya") in kinds
        assert not any(e["type"] == "busy" and e.get("bot") for e in events)
        done = next(e for e in events if e["type"] == "bot_done")
        assert done["report"].startswith("Found 3 PM jobs") and done["ok"]
        saved = scheduler.find("maya")
        assert saved["runs"][-1]["task"] == "find 3 PM jobs in Dubai" and "covered Swiggy" in saved["notes"][0]
    asyncio.run(go())


def test_bot_tasks_queue_and_at_most_two_bots_work_at_once():
    async def go():
        hub, events, made = _hub()
        a, b, c = (scheduler.create(n, "job") for n in ("A", "B", "C"))
        assert await hub.assign(a["id"], "one") == "started"
        assert await hub.assign(a["id"], "two") == "queued"                  # the same bot: after its current task
        assert await hub.assign(b["id"], "x") == "started"
        assert await hub.assign(c["id"], "y") == "queued"                    # two bots are already working
        assert hub.bot_state(a["id"])["queued"] == 1
        await _settle(hub)
        assert [r["text"].split("New task for you")[1].split(": ", 1)[1].split("\n")[0] for r in made["A"].runs] == ["one", "two"]
        assert len(made["C"].runs) == 1
        assert [e["bot"] for e in events if e["type"] == "bot_started"].count("A") == 2
    asyncio.run(go())


def test_stopping_a_bot_only_refuses_its_own_approvals(monkeypatch):
    from karya import ext_link
    monkeypatch.setattr(ext_link.link, "notify", lambda *a, **k: None)

    async def go():
        hub, events, made = _hub(lambda: BotAgent(ask_first=True, wait=0.01))
        maya = scheduler.create("Maya", "Email recruiters")
        main_answer = asyncio.create_task(hub.confirm({"id": "m", "summary": "chat: post on X"}))
        await hub.assign(maya["id"], "email the 3 recruiters")
        while len(hub.pending) < 2:
            await asyncio.sleep(0.01)
        bot_request = next(r for _, r in hub.pending.values() if r.get("bot"))
        assert bot_request["bot"] == "Maya" and bot_request["source"] == "assigned"
        assert hub.stop_bot(maya["id"]) is True
        await _settle(hub)
        assert made["Maya"].answers == [False] and made["Maya"].cancelled
        assert not main_answer.done()                                        # the chat's approval still waits
        hub.deny_all()
        assert await main_answer is False
        assert hub.stop_bot(maya["id"]) is False
    asyncio.run(go())


def test_a_bots_approval_gives_up_when_nobody_answers(monkeypatch):
    monkeypatch.setattr(scheduler, "UNATTENDED_MINUTES", 0.002)
    from karya import ext_link
    monkeypatch.setattr(ext_link.link, "notify", lambda *a, **k: None)

    async def go():
        hub, events, made = _hub(lambda: BotAgent(ask_first=True, wait=0.01))
        maya = scheduler.create("Maya", "Email recruiters")
        await hub.assign(maya["id"], "email them")                           # from the chat: still times out
        await _settle(hub)
        assert made["Maya"].answers == [False]
    asyncio.run(go())


def test_a_real_bot_agent_keeps_its_own_history_and_not_the_users_words(tmp_path, monkeypatch):
    from karya import answers
    noted = []
    monkeypatch.setattr(answers, "note_user_message", lambda text: noted.append(text))
    maya = scheduler.create("Maya", "Research")
    bot = Agent(llm=FakeLLM([reply("Done.\nRemember: x")]), history_file=scheduler.history_path(maya["id"]), bot=maya)
    assert bot.lane == "bots" and not bot.note_user

    async def nothing(event):
        return None

    async def go():
        return await bot.run(scheduler.run_text(maya, "started Mar 2025 at Acme"), nothing, None,
                             user_words="compare two phones")
    assert asyncio.run(go()).startswith("Done.")
    assert noted == ["compare two phones"]                                   # only the user's own words count
    assert scheduler.history_path(maya["id"]).exists()


# ---------------------------------------------------------------- the shared browser
def test_desk_one_run_at_a_time_and_the_user_comes_first(monkeypatch):
    async def go():
        d = desk.Desk()
        maya = runctx.Run(id="b1", source="assigned", bot="Maya", agent_id="a1")
        rex = runctx.Run(id="b2", source="assigned", bot="Rex", agent_id="a2")
        chat = runctx.Run()
        assert await d.acquire(maya, doing="apply to Swiggy") is None and d.status()["bot"] == "Maya"
        assert await d.acquire(maya) is None                               # it keeps it during its run
        blocked = await d.acquire(chat, wait=0.05)
        assert blocked.startswith("NOT RUN") and "Maya" in blocked and "apply to Swiggy" in blocked
        assert "stop Maya" in blocked
        assert (await d.acquire(rex, wait=0.05)).startswith("NOT RUN: Karya's browser is busy (the bot Maya")
        d.release("b1")
        assert await d.acquire(chat, wait=0.05) is None                    # the chat uses it (it never holds it)
        assert "the user is using Karya's browser" in await d.acquire(rex, wait=0.05)
        monkeypatch.setattr(desk, "MAIN_GRACE", 0.0)                        # the user has been away for a while
        assert await d.acquire(rex, wait=0.05) is None
    asyncio.run(go())


def test_which_tools_use_the_desk(monkeypatch):
    assert desk.needs_desk("browser_open", "browser") and desk.needs_desk("find_jobs", "jobs")
    assert desk.needs_desk("market_sentiment", "finance") and desk.needs_desk("crawl_site", "web")
    assert not desk.needs_desk("web_search", "web") and not desk.needs_desk("stock_quote", "finance")
    from karya.config import settings
    monkeypatch.setattr(type(settings), "email_ready", property(lambda self: True))
    assert not desk.needs_desk("send_email", "email")
    monkeypatch.setattr(type(settings), "email_ready", property(lambda self: False))
    assert desk.needs_desk("send_email", "email")                          # then it uses Gmail in the browser


def test_the_chat_waits_for_a_bot_on_the_browser_then_says_who_has_it(monkeypatch):
    monkeypatch.setattr(desk, "MAIN_WAIT", 0.2)
    d = desk.Desk()
    monkeypatch.setattr(desk, "DESK", d)
    holder = runctx.Run(id="b9", source="assigned", bot="Maya", agent_id="a9")
    asyncio.run(d.acquire(holder))
    ran = []
    from karya.tools import browser
    monkeypatch.setattr(browser, "browser_open", lambda **kw: ran.append(kw) or "opened")
    from karya.registry import TOOLS
    monkeypatch.setattr(TOOLS["browser_open"], "func", lambda **kw: ran.append(kw) or "opened")
    llm = FakeLLM([tool_call("browser_open", {"url": "https://example.com"}), reply("The browser is busy.")])
    agent = Agent(llm=llm, persist=False)
    events = []

    async def emit(event):
        events.append(event)

    async def confirm(request):
        return True
    assert asyncio.run(agent.run("open example.com", emit, confirm)) == "The browser is busy."
    result = next(m for m in agent.history if m.get("role") == "tool")["content"]
    assert ran == [] and result.startswith("NOT RUN: Karya's browser is busy: the user's bot Maya")
    assert any(e["type"] == "status" and "Maya is using it" in e["text"] for e in events)


# ---------------------------------------------------------------- WhatsApp
class Bridge:
    state = "ready"

    def __init__(self):
        self.out = []

    def send(self, text):
        self.out.append(text)


def _channel():
    hub, events, made = _hub()
    ch = phone.PhoneChannel(hub, None)
    ch.bridge = Bridge()
    return hub, ch, made


def test_whatsapp_gives_tasks_to_bots_and_stops_them():
    async def go():
        hub, ch, made = _channel()
        maya = scheduler.create("Maya", "Find PM jobs")
        await ch.on_message("@Maya find 2 PM jobs in Pune")
        assert ch.bridge.out[-1].startswith("Maya is on it") and hub.bot_running(maya["id"])
        assert hub.agent.seen == []                                          # not the chat's task
        await ch.on_message("status")
        assert 'Maya: working on "find 2 PM jobs in Pune"' in ch.bridge.out[-1]
        await ch.on_message("stop maya")
        assert ch.bridge.out[-1] == "Stopped Maya." and made["Maya"].cancelled
        await ch.on_message("stop rex")
        assert "don't have a bot called 'rex'" in ch.bridge.out[-1]
        await _settle(hub)
        await ch.on_bot_done(maya, "find", "Found 2 jobs.", "assigned")
        assert ch.bridge.out[-1] == "Maya: Found 2 jobs."
        await ch.on_message("bots")
        assert "Maya: Find PM jobs (works when you give it a task)" in ch.bridge.out[-1]
    asyncio.run(go())


def test_whatsapp_a_bare_yes_never_becomes_a_task():
    async def go():
        hub, ch, made = _channel()
        await ch.on_message("yes")
        assert ch.bridge.out[-1] == "Nothing is waiting for your OK right now." and hub.agent.seen == []
    asyncio.run(go())


def test_whatsapp_approvals_one_at_a_time_with_the_bots_name():
    async def go():
        hub, ch, made = _channel()
        loop = asyncio.get_running_loop()
        f1, f2, f3 = (loop.create_future() for _ in range(3))
        hub.pending.update({"r1": (f1, {}), "r2": (f2, {}), "r3": (f3, {})})
        await ch.on_confirm({"id": "r1", "summary": "Submit to Swiggy", "bot": "Maya", "source": "assigned"})
        await ch.on_confirm({"id": "r2", "summary": "Send the email", "bot": "Rex", "source": "schedule"})
        assert len(ch.bridge.out) == 1 and ch.bridge.out[0].startswith("Maya asks: Approve this?")
        await ch.on_message("yes")
        assert f1.result() is True and ch.bridge.out[-2] == "Approved (Maya)."
        assert ch.bridge.out[-1].startswith("Rex asks: Approve this?\nSend the email")
        await ch.on_confirm({"id": "r3", "summary": "Post on X", "bot": "Rex", "source": "schedule"})
        hub.pending.pop("r2")
        await ch.on_confirm_done("r2", True)                                 # answered on the PC
        assert ch.bridge.out[-2] == "(That one was answered on the PC.)"
        assert ch.bridge.out[-1].startswith("Rex asks: Approve this?\nPost on X")
        await ch.on_message("no")
        assert f3.result() is False and not f2.done()
        await ch.on_confirm({"id": "r4", "summary": "chat thing", "source": "chat"})
        assert ch.waiting == []                                              # the chat's own: on the PC only
    asyncio.run(go())


# ---------------------------------------------------------------- each bot has its own job list
def test_a_bots_pick_list_and_shortlist_never_touch_the_chats():
    from karya import apply_queue
    from karya.tools import jobs
    chat_picks = apply_queue.start([{"id": "J64", "title": "Intern", "company": "ING", "url": "https://ing.example/1"}])
    assert [j["id"] for j in apply_queue.pending()] == ["J64"]
    token = runctx.RUN.set(runctx.Run(id="b1", source="assigned", bot="Maya", agent_id="a1"))
    try:
        assert apply_queue.pending() == []                                  # Maya starts with her own, empty list
        apply_queue.start([{"id": "J2", "title": "PM", "company": "Acme", "url": "https://acme.example/2"}])
        apply_queue.set_current({"id": "J2", "url": "https://acme.example/2"})
        assert [j["id"] for j in apply_queue.pending()] == ["J2"]
        assert jobs.last_jobs_file().name == "last_jobs.a1.json"
    finally:
        runctx.RUN.reset(token)
    assert [j["id"] for j in apply_queue.pending()] == ["J64"] == [j["id"] for j in chat_picks]
    assert jobs.last_jobs_file().name == "last_jobs.json"
    assert apply_queue.current_for("https://acme.example/2") is None       # Maya's current job isn't the chat's
