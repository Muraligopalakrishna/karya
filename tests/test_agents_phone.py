"""Background agents (karya/scheduler.py), tasks from WhatsApp (karya/whatsapp.py, karya/phone.py) and the Hub's queue."""
import asyncio
import time
from collections import deque
from datetime import datetime

import pytest

from karya import phone, scheduler, whatsapp
from karya.server import Hub


@pytest.fixture()
def agents_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "AGENTS_FILE", tmp_path / "agents.json")
    return tmp_path / "agents.json"


# ---------------------------------------------------------------- schedules
@pytest.mark.parametrize("text,want", [("9", "09:00"), ("9:30", "09:30"), ("6:30 pm", "18:30"), ("12am", "00:00"),
                                       ("12 pm", "12:00"), ("18.45", "18:45"), ("25:00", None), ("soon", None)])
def test_clock(text, want):
    assert scheduler.clock(text) == want


def test_daily_runs_at_the_next_time_on_the_right_days():
    monday_8 = datetime(2026, 10, 12, 8, 0)          # a Monday
    agent = {"daily_at": ["09:00", "18:00"], "weekdays": None}
    assert datetime.fromtimestamp(scheduler.next_run(agent, monday_8)) == datetime(2026, 10, 12, 9, 0)
    assert datetime.fromtimestamp(scheduler.next_run(agent, datetime(2026, 10, 12, 9, 30))) == datetime(2026, 10, 12, 18, 0)
    assert datetime.fromtimestamp(scheduler.next_run(agent, datetime(2026, 10, 12, 19, 0))) == datetime(2026, 10, 13, 9, 0)
    only_mon = {"daily_at": ["09:00"], "weekdays": ["mon"]}
    assert datetime.fromtimestamp(scheduler.next_run(only_mon, datetime(2026, 10, 13, 8, 0))) == datetime(2026, 10, 19, 9, 0)


def test_interval_runs_follow_the_last_run_and_never_too_often():
    now = datetime(2026, 10, 12, 10, 0)
    first = scheduler.next_run({"every_minutes": 60}, now)
    assert 0 < first - now.timestamp() <= 120                       # a new agent starts soon
    last = datetime(2026, 10, 12, 9, 30).timestamp()
    assert scheduler.next_run({"every_minutes": 60, "last_run": last}, now) == datetime(2026, 10, 12, 10, 30).timestamp()
    tiny = scheduler.next_run({"every_minutes": 1, "last_run": now.timestamp()}, now)
    assert tiny - now.timestamp() == scheduler.MIN_EVERY * 60


def test_weekdays_words():
    assert scheduler._days(["weekdays"]) == ["mon", "tue", "wed", "thu", "fri"]
    assert scheduler._days(["Saturday", "sun", "nope"]) == ["sat", "sun"]


def test_create_list_update_delete(agents_file):
    out = scheduler.create_agent("Job hunter", "Find new PM jobs in Hyderabad and apply to the best 3",
                                 daily_at=["9am"], weekdays=["weekdays"])
    assert out["created"]["when"] == "daily at 09:00 on Mon, Tue, Wed, Thu, Fri"
    assert scheduler.create_agent("job hunter", "again").startswith("ERROR")          # names are unique
    assert scheduler.create_agent("Gold", "Check gold sentiment", every_minutes=120)["created"]["when"] == "every 2 h"
    assert [a["name"] for a in scheduler.list_agents()] == ["Job hunter", "Gold"]
    paused = scheduler.update_agent("gold", enabled=False)["updated"]
    assert paused["next_run"] == "paused" and not scheduler.due(time.time() + 10 ** 7)[1:]
    scheduler.update_agent("Job hunter", run_now=True)
    assert [a["name"] for a in scheduler.due()] == ["Job hunter"]
    scheduler.started(scheduler.find("job hunter")["id"])
    assert scheduler.due() == []                                     # not picked twice
    scheduler.finished(scheduler.find("job hunter")["id"], "Applied to 2 jobs")
    assert "Applied to 2 jobs" in scheduler.list_agents()[0]["last_report"]
    assert scheduler.delete_agent("Gold").startswith("Deleted")
    assert scheduler.update_agent("Gold", enabled=True).startswith("ERROR")


def test_agent_run_text_asks_for_a_report(agents_file):
    agent = scheduler.create("Gold", "Check gold sentiment")
    text = scheduler.run_text(agent)
    assert text.startswith('[Background agent "Gold"') and "Check gold sentiment" in text and "report" in text


# ---------------------------------------------------------------- WhatsApp messages
def _msgs(*items, chat="917000000000"):
    """WhatsApp message ids carry the chat's own number: the user's number in their "Message yourself" chat."""
    return [{"id": f"{'true' if mine else 'false'}_{chat}@c.us_{mid}", "mine": mine, "text": text}
            for mid, mine, text in items]


def test_whatsapp_only_new_messages_from_the_user_become_tasks():
    seen, seen_set, sent = deque(maxlen=400), set(), deque(maxlen=60)
    old = _msgs(("A", True, "an old note"), ("B", True, "[Karya] Done."))
    assert whatsapp.new_commands(old, seen, seen_set, sent, primed=False) == []      # history is never replayed
    sent.append(whatsapp._norm("[Karya] On it."))
    later = old + _msgs(("C", True, "find PM jobs in Dubai"), ("D", True, "[Karya] On it."), ("E", False, "hi"),
                        ("F", True, "On it."))
    assert whatsapp.new_commands(later, seen, seen_set, sent, primed=True) == ["find PM jobs in Dubai", "On it."]
    assert whatsapp.new_commands(later, seen, seen_set, sent, primed=True) == []       # each one once


def test_whatsapp_a_rerendered_chat_is_not_replayed():
    seen, seen_set, sent = deque(maxlen=400), set(), deque(maxlen=60)
    whatsapp.new_commands(_msgs(("A", True, "one")), seen, seen_set, sent, primed=False)
    other = _msgs(("X", True, "something from last week"), ("Y", True, "and more"))   # nothing known on screen
    assert whatsapp.new_commands(other, seen, seen_set, sent, primed=True) == []
    assert whatsapp.new_commands(other + _msgs(("Z", True, "new task")), seen, seen_set, sent, primed=True) == ["new task"]


def test_whatsapp_self_chat_check():
    assert whatsapp.is_self_chat({"title": "Asha (You)"}, "917000000000")
    assert whatsapp.is_self_chat({"title": "Me", "messages": _msgs(("A", True, "x"))}, "917000000000")
    mom = _msgs(("A", False, "x"), ("B", True, "y"), chat="919800000000")
    assert not whatsapp.is_self_chat({"title": "Mom", "messages": mom}, "917000000000")
    assert not whatsapp.is_self_chat({"title": "Mom", "messages": _msgs(("A", True, "x"))}, "")


class FakePage:
    """WhatsApp Web as the bridge sees it: a QR screen first, then the user's own chat."""

    def __init__(self, bridge, screens):
        self.bridge, self.screens, self.typed, self.gotos = bridge, list(screens), [], []
        self.keyboard = self

    def evaluate(self, js):
        screen = self.screens.pop(0)
        if not self.screens:
            self.bridge.stop()                  # the loop ends after this screen
        return screen

    def goto(self, url, **kw):
        self.gotos.append(url)

    def locator(self, sel):
        return self

    @property
    def last(self):
        return self

    def click(self, timeout=None):
        pass

    def insert_text(self, text):
        self.typed.append(text)

    def press(self, key):
        self.typed.append(f"<{key}>")

    def wait_for_timeout(self, ms):
        pass


def test_whatsapp_bridge_links_then_takes_tasks_and_replies(monkeypatch, tmp_path):
    monkeypatch.setattr(whatsapp, "POLL_SECONDS", 0)
    tasks = []
    bridge = whatsapp.WhatsAppBridge("917000000000", tasks.append)
    bridge.send("Agent \"Gold\": a report from before the link")       # queued while unlinked: dropped
    chat = lambda *m: {"state": "chat", "title": "Asha (You)", "box": True, "messages": _msgs(*m)}  # noqa: E731
    page = FakePage(bridge, [
        {"state": "qr"},
        {"state": "app"},                                                # chat list: opens the self chat
        chat(("A", True, "an old note")),
        chat(("A", True, "an old note"), ("B", True, "find PM jobs in Dubai")),
        {"state": "chat", "title": "Mom", "box": True, "messages": _msgs(("C", True, "hi mom"), chat="919800000000")},
    ])
    bridge._loop(page)
    assert tasks == ["find PM jobs in Dubai"]                            # not the old note, not Mom's chat
    assert page.gotos == ["https://web.whatsapp.com/send?phone=917000000000"]
    sent = "".join(page.typed)
    assert "[Karya] Linked." in sent and "before the link" not in sent
    assert bridge.state == "opening"                                     # Mom's chat: waits for the user's own


def test_owner_number_from_profile(monkeypatch):
    from karya import memory
    store = {"profile": {"phone": "70000 00000", "location": "Hyderabad, India"}}
    monkeypatch.setattr(memory.memory_store, "load", lambda: store)
    assert phone.owner_number() == "917000000000"
    store["profile"] = {"whatsapp": "+44 7700 900123"}
    assert phone.owner_number() == "447700900123"
    store["profile"] = {}
    assert phone.owner_number() == ""


# ---------------------------------------------------------------- the phone channel
class FakeBridge:
    state = "ready"

    def __init__(self):
        self.out = []

    def send(self, text):
        self.out.append(text)


class FakeAgent:
    def __init__(self, final="Done: applied to 2 jobs."):
        self.busy, self.final, self.cancelled, self.seen = False, final, False, []

    async def run(self, text, emit, confirm, ask):
        self.busy = True
        self.seen.append(text)
        await asyncio.sleep(0.01)
        self.busy = False
        return self.final

    def cancel(self):
        self.cancelled = True


def _channel():
    hub = Hub(FakeAgent())
    ch = phone.PhoneChannel(hub, None)
    ch.bridge = FakeBridge()
    return hub, ch


def test_phone_task_starts_or_queues():
    async def go():
        hub, ch = _channel()
        await ch.on_message("find PM jobs in Dubai and apply")
        assert ch.bridge.out[-1].startswith("On it")
        assert hub.source == "phone"
        await ch.on_message("and check gold")                   # still busy
        assert ch.bridge.out[-1].startswith("Queued") and len(hub.queue) == 1
        while hub.busy_now() or hub.queue:
            await asyncio.sleep(0.01)
        assert hub.agent.seen == ["find PM jobs in Dubai and apply", "and check gold"]
    asyncio.run(go())


def test_phone_approvals_only_for_phone_and_agent_tasks():
    async def go():
        hub, ch = _channel()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        hub.pending["c1"] = (future, {})
        await ch.on_confirm({"id": "c1", "summary": "Post on X: hello"})
        assert ch.bridge.out == []                              # a chat task: approve on the PC
        hub.source = "phone"
        await ch.on_confirm({"id": "c1", "summary": "Post on X: hello"})
        assert "Approve this?" in ch.bridge.out[-1] and "Post on X" in ch.bridge.out[-1]
        await ch.on_message("yes go ahead")
        assert future.result() is True and ch.bridge.out[-1].startswith("Approved")
        future2 = loop.create_future()
        hub.pending["c2"] = (future2, {})
        await ch.on_confirm({"id": "c2", "summary": "Submit"})
        await ch.on_message("no")
        assert future2.result() is False
    asyncio.run(go())


def test_phone_picks_jobs_by_number_and_answers_questions():
    async def go():
        hub, ch = _channel()
        hub.source = "agent:a1"
        loop = asyncio.get_running_loop()
        jobs = [{"id": f"J{n}", "title": f"PM {n}", "company": f"Co{n}", "location": "Dubai", "match": 80} for n in range(1, 5)]
        f1 = loop.create_future()
        hub.asks["k1"] = (f1, {})
        await ch.on_ask({"id": "k1", "kind": "jobs", "jobs": jobs})
        assert "1. PM 1 - Co1 (Dubai), 80%" in ch.bridge.out[-1]
        await ch.on_message("hmm")                              # not numbers: asked again, nothing picked
        assert not f1.done()
        await ch.on_message("1 and 3")
        assert f1.result() == {"picked": ["J1", "J3"], "skip_companies": []}
        f2 = loop.create_future()
        hub.asks["k2"] = (f2, {})
        await ch.on_ask({"id": "k2", "kind": "questions", "questions": [{"q": "Notice period?"}, {"q": "Expected CTC?"}]})
        await ch.on_message("1. Immediately\n2. 12 LPA")
        assert f2.result() == {"answers": {"Notice period?": "Immediately", "Expected CTC?": "12 LPA"}}
        f3 = loop.create_future()
        hub.asks["k3"] = (f3, {})
        await ch.on_ask({"id": "k3", "kind": "credentials", "site": "workday.com"})
        assert "passwords never go over WhatsApp" in ch.bridge.out[-1]
    asyncio.run(go())


def test_phone_commands_and_reports():
    async def go():
        hub, ch = _channel()
        await ch.on_message("STOP")
        assert ch.bridge.out[-1] == "Nothing is running."
        await ch.on_message("status")
        assert ch.bridge.out[-1].startswith("Free right now")
        await ch.on_done("chat", "x", "chat answer")            # chat tasks report in the chat only
        assert ch.bridge.out[-1].startswith("Free right now")
        await ch.on_done("phone", "x", "Applied to 2 jobs.")
        assert ch.bridge.out[-1] == "Applied to 2 jobs."
        hub.agent.busy = True
        await ch.on_message("stop")
        assert hub.agent.cancelled and ch.bridge.out[-1] == "Stopped."
    asyncio.run(go())


def test_secrets_never_go_to_whatsapp(monkeypatch):
    from karya import secrets_filter
    monkeypatch.setattr(secrets_filter, "scrub", lambda text: text.replace("hunter2", "[hidden]"))
    _, ch = _channel()
    ch.say("your password is hunter2")
    assert ch.bridge.out == ["your password is [hidden]"]


# ---------------------------------------------------------------- the hub
def test_hub_reports_to_listeners_and_shows_phone_tasks_in_chat():
    async def go():
        hub = Hub(FakeAgent("All done."))
        events, done = [], []

        class Listener:
            async def on_done(self, source, text, final):
                done.append((source, final))

        hub.listeners.append(Listener())

        async def capture(event):
            events.append(event)
        hub.send = capture
        assert await hub.submit("check gold", source="phone", label="WhatsApp") == "started"
        assert await hub.submit("second", source="agent:a1", label="Gold") == "queued"
        while hub.busy_now() or hub.queue:
            await asyncio.sleep(0.01)
        assert done == [("phone", "All done."), ("agent:a1", "All done.")]
        assert {"type": "user", "text": "check gold", "via": "WhatsApp"} in events
        assert hub.source == "chat"
    asyncio.run(go())


def test_unattended_approval_is_refused_after_the_limit(monkeypatch):
    monkeypatch.setattr(scheduler, "UNATTENDED_MINUTES", 0.002)
    from karya import ext_link
    monkeypatch.setattr(ext_link.link, "notify", lambda *a, **k: None)

    async def go():
        hub = Hub(FakeAgent())
        hub.source = "agent:a1"
        assert await hub.confirm({"summary": "Submit application"}) is False
        assert await hub.ask({"kind": "questions", "questions": []}) is None
        hub.source = "chat"                                     # at the PC: waits for the user
        task = asyncio.create_task(hub.confirm({"summary": "Submit"}))
        await asyncio.sleep(0.3)
        assert not task.done()
        hub.deny_all()
        assert await task is False
    asyncio.run(go())
