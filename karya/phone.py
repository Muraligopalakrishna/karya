"""Tasks from your phone, and Karya's approvals, questions and reports there (WhatsApp; see whatsapp.py).

A message you send yourself on WhatsApp becomes a Karya task (queued if the chat is busy). "@Maya find PM jobs" (or
"Maya: ...") gives the task to that bot instead. While a task from the phone, or any bot, waits for an approval or an
answer, the question comes to WhatsApp too, one at a time, with the bot's name: reply YES / NO, the numbers of the
jobs to apply to, or your answers. Logins and passwords never go over WhatsApp: those stay on the PC. STATUS, STOP,
STOP <bot>, BOTS and HELP are understood as commands."""
from __future__ import annotations

import asyncio
import re
import time

from .config import settings
from .registry import CONFIRM, P, tool

CHANNEL: "PhoneChannel | None" = None
_YES = re.compile(r"^\W*(y|yes|yeah|yep|yup|ok|okay|approve[d]?|go( ahead)?|do it|sure|haan?|ha|han|ji)\b", re.I)
_NO = re.compile(r"^\W*(n|no|nope|deny|denied|don'?t|do not|cancel|nahi|nah|na)\b", re.I)
_TO_KARYA = re.compile(r"^\s*(?:\[\s*karya\s*\]|@karya\b|karya\s*[:,\-])\s*", re.I)   # "[Karya] ...", "Karya, ..."
_BARE = re.compile(r"^\W*(y|yes|yeah|yep|ok|okay|approve[d]?|sure|n|no|nope|deny|nahi|na)\W*$", re.I)
HELP = ("Send me any task, e.g. \"find PM jobs in Dubai and apply\" or \"what's the gold sentiment\". Give a task to "
        "one of your bots with \"@Name task\" (e.g. \"@Maya apply to 3 new PM jobs\"). Commands: STATUS (what's "
        "running), STOP (stop the chat's task), STOP Name (stop a bot), BOTS (your bots).")


def owner_number() -> str:
    """The user's WhatsApp number with country code, digits only, from their profile ('' when unknown)."""
    from .memory import memory_store
    prof = memory_store.load().get("profile", {})
    digits = re.sub(r"\D", "", str(prof.get("whatsapp") or prof.get("phone") or ""))
    if len(digits) == 10 and re.search(r"india", str(prof.get("location") or ""), re.I):
        digits = "91" + digits
    return digits if 10 <= len(digits) <= 15 else ""


class PhoneChannel:
    def __init__(self, hub, loop):
        self.hub, self.loop = hub, loop
        self.bridge = None
        self.waiting: list[dict] = []    # approvals and questions sent here, oldest first (answered in that order)

    # ----- linking
    def enable(self, wait: float = 25.0) -> dict:
        from .whatsapp import WhatsAppBridge
        owner = owner_number()
        if not owner:
            return {"state": "error", "detail": "Karya needs your WhatsApp number with country code. Save it with "
                                                "update_profile(field='whatsapp', value='+91...') and connect again."}
        if self.bridge is None or self.bridge.owner != owner:
            if self.bridge is not None:
                self.bridge.stop()
            self.bridge = WhatsAppBridge(owner, self._incoming)
        self.bridge.start()
        try:
            from .config import update_env
            update_env({"WHATSAPP_ENABLED": "true"})
        except (OSError, ValueError):
            pass
        end = time.time() + wait
        while time.time() < end and self.bridge.state in ("starting", "opening"):
            time.sleep(1)
        return self.status()

    def disable(self) -> dict:
        if self.bridge is not None:
            self.bridge.stop()
        try:
            from .config import update_env
            update_env({"WHATSAPP_ENABLED": "false"})
        except (OSError, ValueError):
            pass
        return {"state": "off"}

    def status(self) -> dict:
        return self.bridge.status() if self.bridge is not None else {"state": "off", "detail": ""}

    def say(self, text: str) -> None:
        if self.bridge is not None and self.bridge.state != "off" and str(text or "").strip():
            from .secrets_filter import scrub
            self.bridge.send(scrub(str(text)))

    def _incoming(self, text: str) -> None:          # from the WhatsApp thread
        asyncio.run_coroutine_threadsafe(self.on_message(text), self.loop)

    # ----- Karya -> phone (hub listener: bots always, the chat's own run only for tasks from the phone)
    def _routed(self, request: dict) -> bool:
        if request.get("bot"):
            return True
        return str(request.get("source") or getattr(self.hub, "source", "chat")) != "chat"

    def _prompt(self, entry: dict) -> None:
        request = entry["request"]
        who = f"{request['bot']} asks: " if request.get("bot") else ""
        more = len(self.waiting) - 1
        tail = f"\n({more} more waiting after this one.)" if more > 0 else ""
        entry["prompted"] = True
        if entry["kind"] == "confirm":
            self.say(f"{who}Approve this?\n{str(request.get('summary') or 'an action')[:900]}\n\nReply YES or NO.{tail}")
        elif request.get("kind") == "code":
            self.say(f"{who}{request.get('site') or 'A site'} sent you a one-time login code (by text message or email) "
                     f"and I couldn't find it in your email. Reply with just the code.{tail}")
        elif request.get("kind") == "jobs":
            rows = entry["rows"]
            lines = [f"{n}. {r.get('title', '')[:70]} - {r.get('company', '')} ({r.get('location') or '?'}), "
                     f"{r.get('match', '?')}%" for n, r in enumerate(rows, 1)]
            self.say(f"{who}Which jobs should I apply to? Reply with numbers (e.g. 1 3 5), ALL or NONE.\n"
                     + "\n".join(lines) + tail)
        else:
            qs = request.get("questions") or []
            lines = [f"{n}. {q.get('q', '')}" + (f" (e.g. {q.get('hint') or (q.get('options') or [''])[0]})"
                                                   if q.get("hint") or q.get("options") else "")
                     for n, q in enumerate(qs, 1)]
            self.say(who + ("I need your answer:\n" if len(qs) == 1 else
                            "I need your answers, one line each, in this order:\n") + "\n".join(lines) + tail)

    def _add(self, entry: dict) -> None:
        self.waiting.append(entry)
        if len(self.waiting) == 1:
            self._prompt(entry)

    def _resolved(self, request_id: str, elsewhere: bool) -> None:
        first = self.waiting[0] if self.waiting else None
        self.waiting = [e for e in self.waiting if e["request"].get("id") != request_id]
        if first is not None and first["request"].get("id") == request_id:
            if elsewhere and first.get("prompted"):
                self.say("(That one was answered on the PC.)")
            if self.waiting and not self.waiting[0].get("prompted"):
                self._prompt(self.waiting[0])

    async def on_confirm(self, request: dict) -> None:
        if self._routed(request):
            self._add({"kind": "confirm", "request": request})

    async def on_ask(self, request: dict) -> None:
        if not self._routed(request):
            return
        kind = request.get("kind")
        if kind in ("jobs", "questions", "code"):
            self._add({"kind": "ask", "request": request, "rows": (request.get("jobs") or [])[:15]})
        else:                                        # a login or anything secret: on the PC only
            who = request.get("bot") or "Karya"
            self.say(f"{who} needs a login (or something private) for this task. Please answer it in Karya's window "
                     "on your PC; passwords never go over WhatsApp.")

    async def on_confirm_done(self, request_id: str, approved: bool) -> None:
        self._resolved(request_id, elsewhere=True)

    async def on_ask_done(self, request_id: str, answered: bool) -> None:
        self._resolved(request_id, elsewhere=True)

    async def on_done(self, source: str, text: str, final: str) -> None:
        self.waiting = [e for e in self.waiting if e["request"].get("bot")]   # the chat's run is over
        if source == "chat":
            return
        self.say(str(final or "Done.").strip()[:3400])

    async def on_bot_done(self, record: dict, task: str, report: str, source: str) -> None:
        self.waiting = [e for e in self.waiting if e["request"].get("bot") != record.get("name")]
        self.say(f"{record.get('name', 'Your bot')}: {str(report or 'Done.').strip()}"[:3400])

    # ----- phone -> Karya
    async def on_message(self, text: str) -> None:
        text = str(text or "").strip()
        text = _TO_KARYA.sub("", text, count=1).strip() or text
        low = text.lower()
        hub = self.hub
        self.waiting = [e for e in self.waiting
                        if e["request"].get("id") in (hub.pending if e["kind"] == "confirm" else hub.asks)]
        first = self.waiting[0] if self.waiting else None
        if first and first["kind"] == "confirm" and (_YES.match(low) or _NO.match(low)):
            approved = bool(_YES.match(low)) and not _NO.match(low)
            self.waiting.pop(0)
            hub.answer(first["request"]["id"], approved)
            who = first["request"].get("bot")
            self.say(("Approved" if approved else "OK, not doing that") + (f" ({who})." if who else "."))
            if self.waiting:
                self._prompt(self.waiting[0])
            return
        if first and first["kind"] == "ask" and not re.match(r"^\W*(stop|cancel)\b", low):
            data = self._answer(first, text)
            if data is None:
                kind = first["request"].get("kind")
                self.say("Reply with numbers like 1 3 5, ALL or NONE." if kind == "jobs" else
                         "Please send just the code (4 to 10 letters or digits)." if kind == "code" else
                         "Please send the answers, one line each.")
                return
            self.waiting.pop(0)
            hub.answer_ask(first["request"]["id"], data)
            self.say("Got it.")
            if self.waiting:
                self._prompt(self.waiting[0])
            return
        if _BARE.match(low):                         # a YES/NO with nothing waiting is never a new task
            self.say("Nothing is waiting for your OK right now.")
            return
        stop = re.match(r"^\W*(stop|cancel)\W*(.*)$", low)
        if stop:
            await self._stop(stop.group(2).strip(" .!"))
            return
        if low in ("status", "?", "what are you doing", "what are you doing?"):
            self.say(self._status_text())
            return
        if low in ("help", "hi", "hello", "hey"):
            self.say(HELP)
            return
        if low in ("agents", "my agents", "bots", "my bots"):
            from . import scheduler
            rows = [f"- {a['name']}: {a['task'][:80]} ({scheduler.describe(a)}"
                    f"{'' if a.get('enabled') else ', paused'}{', working now' if hub.bot_running(a['id']) else ''})"
                    for a in scheduler.load()]
            self.say("Your bots:\n" + "\n".join(rows) if rows else "No bots yet. Ask me to make one, e.g. \"make a bot "
                                                                    "called Maya that finds and applies to PM jobs\".")
            return
        from . import scheduler
        target = scheduler.addressed(text)
        if target:
            bot, task = target
            state = await hub.assign(bot["id"], task, source="assigned", user_words=task)
            self.say(f"{bot['name']} is on it. I'll message you here when it's done." if state == "started" else
                     f"{bot['name']} has it queued: it finishes its current work first.")
            return
        result = await hub.submit(text, source="phone", label="WhatsApp")
        self.say("On it. I'll message you here when it's done." if result == "started" else
                 f"Queued: I'm finishing another task first ({len(hub.queue)} waiting).")

    async def _stop(self, name: str) -> None:
        hub = self.hub
        if name and name not in ("it", "all", "that", "this", "everything"):
            from . import scheduler
            bot = scheduler.find(name)
            if bot is None:
                self.say(f"I don't have a bot called {name!r}. Send BOTS to see them.")
            else:
                self.say(f"Stopped {bot['name']}." if hub.stop_bot(bot["id"]) else f"{bot['name']} isn't working on anything.")
            return
        stopped = []
        if hub.busy_now():
            hub.agent.cancel()
            hub.deny_all()
            stopped.append("the chat's task")
        if name in ("all", "everything"):
            for agent_id in list(hub.bots):
                hub.stop_bot(agent_id)
                stopped.append("your bots")
        self.say(("Stopped " + " and ".join(dict.fromkeys(stopped)) + ".") if stopped else
                 ("Nothing is running in the chat." + (" (Bots are working: send STOP ALL or STOP <name>.)" if hub.bots else "")))

    @staticmethod
    def _answer(entry: dict, text: str) -> dict | None:
        ask = entry["request"]
        if ask.get("kind") == "code":
            from .login_codes import clean_code
            code = clean_code(text)
            return {"code": code} if code else None
        if ask.get("kind") == "jobs":
            rows = entry.get("rows") or []
            low = text.lower()
            if re.match(r"^\W*(all|every|sab)\b", low):
                return {"picked": [r["id"] for r in rows], "skip_companies": []}
            if re.match(r"^\W*(none|no|nothing|skip)\b", low):
                return {"picked": [], "skip_companies": []}
            nums = [int(n) for n in re.findall(r"\d+", text)]
            picked = [rows[n - 1]["id"] for n in nums if 1 <= n <= len(rows)]
            return {"picked": list(dict.fromkeys(picked)), "skip_companies": []} if picked else None
        qs = ask.get("questions") or []
        if len(qs) == 1:
            return {"answers": {qs[0]["q"]: text}}
        lines = [re.sub(r"^\s*\d+\s*[.):-]\s*", "", ln).strip() for ln in re.split(r"\n|;", text) if ln.strip()]
        if len(lines) < len(qs):
            return None
        return {"answers": {q["q"]: lines[n] for n, q in enumerate(qs)}}

    def _status_text(self) -> str:
        hub = self.hub
        from . import scheduler
        lines = [f"Chat: working on a task from {getattr(hub, 'label', 'the chat')}." if hub.busy_now() else "Chat: free."]
        if hub.queue:
            lines.append(f"{len(hub.queue)} task(s) waiting for the chat.")
        for row in hub.bots_running():
            lines.append(f"{row['bot']}: working on \"{row['task'][:100]}\"")
        if self.waiting:
            lines.append(f"{len(self.waiting)} thing(s) waiting for your answer here.")
        upcoming = sorted((a for a in scheduler.load() if a.get("enabled") and a.get("next_run")),
                          key=lambda a: a["next_run"])[:3]
        for a in upcoming:
            lines.append(f"Next: {a['name']} at {time.strftime('%a %H:%M', time.localtime(a['next_run']))}")
        return "\n".join(lines)


# ---------------------------------------------------------------- tools
def _need_channel() -> str | None:
    if CHANNEL is None:
        return "ERROR: WhatsApp works only while Karya's app is running (start.bat), not from here."
    return None


@tool("connect_whatsapp", "Link the user's WhatsApp so they can give Karya tasks from their phone: they write in their "
      "own 'Message yourself' chat; Karya replies there, asks there for approvals and sends background agents' "
      "reports there (bots' too). Opens WhatsApp Web in a window of its own; the first time, the user scans the code with "
      "WhatsApp > Linked devices > Link a device. Only that one chat is ever read.", group="agents")
def connect_whatsapp():
    problem = _need_channel()
    if problem:
        return problem
    status = CHANNEL.enable()
    tips = {"needs_qr": "Tell the user: open WhatsApp on your phone > Settings > Linked devices > Link a device, and "
                        "scan the code in the WhatsApp window Karya opened. Then message yourself to give tasks.",
            "ready": "Linked. The user can now message themselves on WhatsApp to give Karya tasks.",
            "error": "It didn't start: tell the user what the detail says."}
    return {**status, "next": tips.get(status.get("state"), "WhatsApp Web is still loading; check whatsapp_status "
                                                              "in a minute.")}


@tool("whatsapp_status", "Whether the user's WhatsApp is linked to Karya (and what to do if not).", group="agents")
def whatsapp_status():
    problem = _need_channel()
    return problem or CHANNEL.status()


@tool("disconnect_whatsapp", "Stop taking tasks from WhatsApp and close Karya's WhatsApp window.", risk=CONFIRM,
      group="agents")
def disconnect_whatsapp():
    problem = _need_channel()
    return problem or CHANNEL.disable()


def start(hub, loop) -> "PhoneChannel":
    """Called by the server at start: the channel exists from then on; WhatsApp opens if the user linked it before."""
    global CHANNEL
    CHANNEL = PhoneChannel(hub, loop)
    if settings.whatsapp_enabled and owner_number():
        import threading
        threading.Thread(target=CHANNEL.enable, kwargs={"wait": 0}, daemon=True, name="karya-whatsapp-start").start()
    return CHANNEL
