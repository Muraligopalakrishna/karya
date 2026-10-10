"""Tasks from your phone, and Karya's approvals, questions and reports there (WhatsApp; see whatsapp.py).

A message you send yourself on WhatsApp becomes a Karya task (queued if Karya is busy). While a task from the phone, or
a background agent's run, waits for an approval or an answer, the question comes to WhatsApp too: reply YES / NO, the
numbers of the jobs to apply to, or your answers. Logins and passwords never go over WhatsApp: those stay on the PC.
STATUS, STOP and HELP are understood as commands."""
from __future__ import annotations

import asyncio
import re
import time

from .config import settings
from .registry import CONFIRM, P, tool

CHANNEL: "PhoneChannel | None" = None
_YES = re.compile(r"^\W*(y|yes|yeah|yep|yup|ok|okay|approve[d]?|go( ahead)?|do it|sure|haan?|ha|han|ji)\b", re.I)
_NO = re.compile(r"^\W*(n|no|nope|deny|denied|don'?t|do not|cancel|nahi|nah|na)\b", re.I)
HELP = ("Send me any task, e.g. \"find PM jobs in Dubai and apply\", \"what's the gold sentiment\", \"every morning at 9 "
        "find new jobs\". Commands: STATUS (what I'm doing), STOP (stop the current task), AGENTS (my background agents).")


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
        self.waiting_confirm: str | None = None
        self.waiting_ask: dict | None = None

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

    # ----- Karya -> phone (hub listener; only for tasks from the phone or a background agent)
    def _routed(self) -> bool:
        return getattr(self.hub, "source", "chat") != "chat"

    async def on_confirm(self, request: dict) -> None:
        if not self._routed():
            return
        self.waiting_confirm = request.get("id")
        self.say(f"Approve this?\n{str(request.get('summary') or 'an action')[:900]}\n\nReply YES or NO.")

    async def on_ask(self, request: dict) -> None:
        if not self._routed():
            return
        kind = request.get("kind")
        if kind == "jobs":
            rows = (request.get("jobs") or [])[:15]
            self.waiting_ask = {**request, "_rows": rows}
            lines = [f"{n}. {r.get('title', '')[:70]} - {r.get('company', '')} ({r.get('location') or '?'}), "
                     f"{r.get('match', '?')}%" for n, r in enumerate(rows, 1)]
            self.say("Which jobs should I apply to? Reply with numbers (e.g. 1 3 5), ALL or NONE.\n" + "\n".join(lines))
        elif kind == "questions":
            qs = request.get("questions") or []
            self.waiting_ask = request
            lines = [f"{n}. {q.get('q', '')}" + (f" (e.g. {q.get('hint') or (q.get('options') or [''])[0]})"
                                                   if q.get("hint") or q.get("options") else "")
                     for n, q in enumerate(qs, 1)]
            self.say(("Karya needs your answer:\n" if len(qs) == 1 else
                      "Karya needs your answers, one line each, in this order:\n") + "\n".join(lines))
        else:                                        # a login or anything secret: on the PC only
            self.say("Karya needs a login (or something private) for this task. Please answer it in Karya's window on "
                     "your PC; passwords never go over WhatsApp.")

    async def on_done(self, source: str, text: str, final: str) -> None:
        self.waiting_confirm = self.waiting_ask = None
        if source == "chat":
            return
        report = str(final or "Done.").strip()
        if source.startswith("agent:"):
            from . import scheduler
            agent = next((a for a in scheduler.load() if a["id"] == source.split(":", 1)[1]), None)
            report = f"Agent \"{(agent or {}).get('name', 'background')}\":\n{report}"
        self.say(report[:3400])

    # ----- phone -> Karya
    async def on_message(self, text: str) -> None:
        text = str(text or "").strip()
        low = text.lower()
        hub = self.hub
        if self.waiting_confirm and self.waiting_confirm in hub.pending and (_YES.match(low) or _NO.match(low)):
            approved = bool(_YES.match(low)) and not _NO.match(low)
            hub.answer(self.waiting_confirm, approved)
            self.waiting_confirm = None
            self.say("Approved, going ahead." if approved else "OK, not doing that.")
            return
        if self.waiting_ask and self.waiting_ask.get("id") in hub.asks and low not in ("stop", "cancel"):
            data = self._answer(text)
            if data is None:
                self.say("Reply with numbers like 1 3 5, ALL or NONE." if self.waiting_ask.get("kind") == "jobs"
                         else "Please send the answers, one line each.")
                return
            hub.answer_ask(self.waiting_ask["id"], data)
            self.waiting_ask = None
            self.say("Got it.")
            return
        if low in ("stop", "cancel", "stop it"):
            if hub.busy_now():
                hub.agent.cancel()
                hub.deny_all()
                self.say("Stopped.")
            else:
                self.say("Nothing is running.")
            return
        if low in ("status", "?", "what are you doing", "what are you doing?"):
            self.say(self._status_text())
            return
        if low in ("help", "hi", "hello", "hey"):
            self.say(HELP)
            return
        if low in ("agents", "my agents"):
            from . import scheduler
            rows = [f"- {a['name']}: {scheduler.describe(a)}{'' if a.get('enabled') else ' (paused)'}" for a in scheduler.load()]
            self.say("Background agents:\n" + "\n".join(rows) if rows else "No background agents yet.")
            return
        result = await hub.submit(text, source="phone", label="WhatsApp")
        self.say("On it. I'll message you here when it's done." if result == "started" else
                 f"Queued: I'm finishing another task first ({len(hub.queue)} waiting).")

    def _answer(self, text: str) -> dict | None:
        ask = self.waiting_ask or {}
        if ask.get("kind") == "jobs":
            rows = ask.get("_rows") or []
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
        lines = [f"Working on a task from {getattr(hub, 'label', 'the chat')}." if hub.busy_now() else "Free right now."]
        if hub.queue:
            lines.append(f"{len(hub.queue)} task(s) waiting.")
        upcoming = sorted((a for a in scheduler.load() if a.get("enabled")), key=lambda a: a.get("next_run") or 0)[:3]
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
      "reports there. Opens WhatsApp Web in a window of its own; the first time, the user scans the code with "
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
