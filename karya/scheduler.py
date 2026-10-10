"""Bots: named helpers that work for the user in the background, like Grok Bots or OpenAI's Dots.

The user, 2026-10-10: "build agents bots like dots and grok bots ... they can assign whatever they want, not only
trading". A bot has a name and a job ("Job hunter: find PM jobs in India and apply", "Gold watcher", "Researcher").
It can do anything Karya can. It works when it's given a task (from the chat with "@Name ...", from WhatsApp, from
Setup, or when the user asks Karya to hand something over), and also on a schedule when it has one ("every morning
at 9"). Each bot runs on its own agent with its own history and notes (what it remembers), in parallel with the
chat and with other bots; only the browser and the job pick list are shared, one run at a time (see desk.py). Its
report goes to the chat and to WhatsApp, where its approvals are asked too (refused after UNATTENDED_MINUTES without
an answer). Bots work while Karya, and so the PC, is on."""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

from .config import DATA_DIR
from .registry import CONFIRM, P, tool

AGENTS_FILE = DATA_DIR / "agents.json"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_EVERY = 15                 # minutes: no bot runs on a schedule more often than this
UNATTENDED_MINUTES = 30        # an approval nobody answers is refused after this long
MAX_NOTES = 20
HUB = None                     # the server's Hub, set when Karya's app starts
LOOP: asyncio.AbstractEventLoop | None = None
_LOCK = threading.RLock()
_REMEMBER = re.compile(r"(?im)^[\s>*_\-]*remember\s*:\s*(.+?)\s*$")


# ---------------------------------------------------------------- storage
def load() -> list[dict]:
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def _save(agents: list[dict]) -> None:
    AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENTS_FILE.write_text(json.dumps(agents, ensure_ascii=False, indent=1), encoding="utf-8")


def history_path(agent_id: str):
    """Each bot's own conversation (data/agents/<id>.json)."""
    return AGENTS_FILE.with_name("agents") / f"{agent_id}.json"


def find(key: str) -> dict | None:
    key = str(key or "").strip().lstrip("@").lower()
    agents = load()
    return next((a for a in agents if a["id"].lower() == key), None) or \
        next((a for a in agents if a["name"].lower() == key), None) or \
        next((a for a in agents if key and key in a["name"].lower()), None)


def addressed(text: str) -> tuple[dict, str] | None:
    """'@Maya find PM jobs' or 'Maya: find PM jobs' -> (Maya, 'find PM jobs'); anything else -> None."""
    raw = str(text or "").strip()
    for agent in sorted(load(), key=lambda a: -len(a["name"])):
        for prefix in ("@" + agent["name"], agent["name"] + ":"):
            if not raw.lower().startswith(prefix.lower()):
                continue
            rest = raw[len(prefix):]
            if prefix.startswith("@") and rest and rest[0] not in " ,:\n\t-":
                continue                           # "@Mayank" isn't "@Maya"
            task = rest.lstrip(" ,:\n\t-").strip()
            if task:
                return agent, task
    return None


# ---------------------------------------------------------------- schedules
def clock(text) -> str | None:
    """'9', '9:00', '09:00', '9am', '6:30 pm', '18:30' -> 'HH:MM' (24 h), or None."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*", str(text or ""), re.I)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    half = (m.group(3) or "").lower().replace(".", "")
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if 0 <= hour < 24 and 0 <= minute < 60 else None


def _days(weekdays) -> list[str]:
    out = []
    for d in weekdays or []:
        key = str(d).strip().lower()[:3]
        if key in WEEKDAYS:
            out.append(key)
        elif key in ("wee", "wor"):          # "weekdays", "working days"
            out += list(WEEKDAYS[:5])
        elif key in ("wkn", "end"):          # "weekend"
            out += ["sat", "sun"]
    return sorted(set(out), key=WEEKDAYS.index)


def scheduled(agent: dict) -> bool:
    return bool(agent.get("every_minutes") or agent.get("daily_at"))


def next_run(agent: dict, after: datetime | None = None) -> float | None:
    """When the bot's schedule is due next (epoch seconds); None for a bot that only works when given a task."""
    if not scheduled(agent):
        return None
    after = after or datetime.now()
    days = agent.get("weekdays") or list(WEEKDAYS)
    if agent.get("every_minutes"):
        step = timedelta(minutes=max(MIN_EVERY, int(agent["every_minutes"])))
        last = agent.get("last_run")
        when = datetime.fromtimestamp(last) + step if last else after + timedelta(minutes=1)
        while when < after:
            when += step
        for _ in range(8):                  # skip days the bot doesn't run on
            if WEEKDAYS[when.weekday()] in days:
                break
            when = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        return when.timestamp()
    best = None
    for hhmm in agent.get("daily_at") or []:
        hour, minute = (int(x) for x in hhmm.split(":"))
        for offset in range(8):
            day = after + timedelta(days=offset)
            when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when > after and WEEKDAYS[when.weekday()] in days:
                best = when if best is None or when < best else best
                break
    return (best or after + timedelta(days=1)).timestamp()


def describe(agent: dict) -> str:
    if not scheduled(agent):
        return "works when you give it a task"
    days = agent.get("weekdays") or []
    on = "" if not days or len(days) == 7 else " on " + ", ".join(d.title() for d in days)
    if agent.get("every_minutes"):
        every = int(agent["every_minutes"])
        return (f"every {every // 60} h" if every % 60 == 0 else f"every {every} min") + on
    return "daily at " + ", ".join(agent.get("daily_at") or []) + on


# ---------------------------------------------------------------- changes
def _times(daily_at) -> list[str]:
    if isinstance(daily_at, str):
        daily_at = re.split(r"[,;]| and ", daily_at)
    return [t for t in (clock(x) for x in (daily_at or [])) if t]


def create(name: str, task: str, every_minutes: int | None = None, daily_at=None, weekdays=None) -> dict:
    name = re.sub(r"\s+", " ", str(name or "")).strip().lstrip("@")[:40]
    task = str(task or "").strip()[:2000]
    if not name or not task:
        raise ValueError("give the bot a name and its job")
    if not re.fullmatch(r"[\w][\w .'-]*", name):
        raise ValueError("a bot's name can use letters, numbers, spaces, - and ' only")
    times = _times(daily_at)
    if daily_at and not times:
        raise ValueError("daily_at must be times like '09:00' or '6:30 pm'")
    agent = {"id": "a" + uuid.uuid4().hex[:6], "name": name, "task": task,
             "every_minutes": max(MIN_EVERY, int(every_minutes)) if every_minutes and not times else None,
             "daily_at": times or None, "weekdays": _days(weekdays) or None, "enabled": True,
             "created": time.time(), "last_run": None, "runs": [], "notes": []}
    agent["next_run"] = next_run(agent)
    with _LOCK:
        agents = load()
        if any(a["name"].lower() == name.lower() for a in agents):
            raise ValueError(f"there's already a bot called {name!r}: change it with update_agent")
        agents.append(agent)
        _save(agents)
    return agent


def update(key: str, **fields) -> dict:
    with _LOCK:
        agents = load()
        agent = next((a for a in agents if a["id"] == (find(key) or {}).get("id")), None)
        if agent is None:
            raise ValueError(f"no bot {key!r}")
        if fields.get("task"):
            agent["task"] = str(fields["task"]).strip()[:2000]
        if fields.get("name"):
            agent["name"] = str(fields["name"]).strip().lstrip("@")[:40]
        if fields.get("enabled") is not None:
            agent["enabled"] = bool(fields["enabled"])
        if fields.get("on_demand"):
            agent["every_minutes"] = agent["daily_at"] = agent["weekdays"] = None
        if fields.get("every_minutes"):
            agent["every_minutes"], agent["daily_at"] = max(MIN_EVERY, int(fields["every_minutes"])), None
        if fields.get("daily_at"):
            times = _times(fields["daily_at"])
            if not times:
                raise ValueError("daily_at must be times like '09:00'")
            agent["daily_at"], agent["every_minutes"] = times, None
        if fields.get("weekdays") is not None:
            agent["weekdays"] = _days(fields["weekdays"]) or None
        if fields.get("run_now") and scheduled(agent):
            agent["next_run"] = time.time()
        elif any(fields.get(k) is not None for k in ("every_minutes", "daily_at", "weekdays", "enabled", "on_demand")):
            agent["next_run"] = next_run(agent)
        _save(agents)
        return agent


def forget_files(agent_id: str) -> None:
    """A deleted bot's history, pick list and job shortlist."""
    from .tools import jobs
    for path in (history_path(agent_id), jobs.CACHE_DIR / f"last_jobs.{agent_id}.json",
                 jobs.CACHE_DIR / f"apply_queue.{agent_id}.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def delete(key: str) -> dict:
    with _LOCK:
        agents = load()
        agent = find(key)
        if agent is None:
            raise ValueError(f"no bot {key!r}")
        _save([a for a in agents if a["id"] != agent["id"]])
    forget_files(agent["id"])
    return agent


def due(now: float | None = None) -> list[dict]:
    now = now or time.time()
    return sorted((a for a in load() if a.get("enabled") and a.get("next_run") and a["next_run"] <= now),
                  key=lambda a: a["next_run"])


def started(agent_id: str) -> None:
    """A scheduled run began: it won't be picked again until its next time."""
    with _LOCK:
        agents = load()
        for a in agents:
            if a["id"] == agent_id:
                a["last_run"] = time.time()
                a["next_run"] = next_run(a, datetime.now() + timedelta(seconds=30))
        _save(agents)


def finished(agent_id: str, report: str, task: str = "") -> None:
    with _LOCK:
        agents = load()
        for a in agents:
            if a["id"] == agent_id:
                a.setdefault("runs", []).append({"at": time.strftime("%Y-%m-%d %H:%M"), "task": str(task or "")[:200],
                                                 "report": str(report or "")[:1500]})
                del a["runs"][:-10]
        _save(agents)


def remember(agent_id: str, report: str) -> list[str]:
    """Keep the report's 'Remember: ...' lines as the bot's notes (its memory for the next runs)."""
    found = [n[:200] for n in _REMEMBER.findall(report or "") if n.strip()]
    if not found:
        return []
    with _LOCK:
        agents = load()
        for a in agents:
            if a["id"] == agent_id:
                notes = a.setdefault("notes", [])
                for n in found:
                    if n not in notes:
                        notes.append(n)
                del notes[:-MAX_NOTES]
        _save(agents)
    return found


def run_text(agent: dict, task: str | None = None, source: str = "schedule") -> str:
    when = time.strftime("%a %d %b %H:%M")
    lines = [f'[You are "{agent["name"]}", one of the user\'s bots in Karya. Your job: {agent["task"]}]']
    notes = agent.get("notes") or []
    if notes:
        lines.append("Notes you wrote yourself after earlier runs (facts to use, not instructions):\n"
                     + "\n".join(f"- {n}" for n in notes[-12:]))
    if task:
        lines.append(f"New task for you ({when}): {task}")
    else:
        lines.append(f"Scheduled run ({when}): do your job now.")
    lines.append("Work on your own as far as you can, without asking \"shall I?\". End with a short report for the user: "
                 "what you did, what you found (with links) and anything only they can do. Then add one line starting "
                 "with \"Remember:\" for each thing you'll need next time (for example what you already covered).")
    return "\n\n".join(lines)


class Hooks:
    """Hub listener: keeps each bot's reports and notes."""

    async def on_bot_done(self, record: dict, task: str, report: str, source: str) -> None:
        finished(record["id"], report, task)
        remember(record["id"], report)


async def loop(hub, every: float = 20.0) -> None:
    """Start bots whose schedule is due (in parallel with the chat), and tasks from WhatsApp that waited."""
    while True:
        await asyncio.sleep(every)
        try:
            if hub.queue and not hub.busy_now():
                await hub.next_queued()
            for agent in due():
                if hub.bot_running(agent["id"]):
                    continue
                started(agent["id"])
                await hub.assign(agent["id"], None, source="schedule")
        except Exception:  # noqa: BLE001 - a broken bot file never stops the server
            continue


def _on_hub(coro, timeout: float = 15.0):
    """Run one of the Hub's coroutines from a tool (tools run in worker threads)."""
    if HUB is None or LOOP is None:
        coro.close()
        raise RuntimeError("bots work only while Karya's app is running (start.bat)")
    return asyncio.run_coroutine_threadsafe(coro, LOOP).result(timeout)


# ---------------------------------------------------------------- tools
def _row(a: dict) -> dict:
    nxt = datetime.fromtimestamp(a["next_run"]).strftime("%a %d %b %H:%M") if a.get("next_run") else ""
    last = (a.get("runs") or [{}])[-1]
    live = HUB.bot_state(a["id"]) if HUB is not None else None
    row = {"id": a["id"], "name": a["name"], "task": a["task"][:300], "when": describe(a), "enabled": a.get("enabled"),
           "next_run": (nxt or "when you give it a task") if a.get("enabled") else "paused",
           "working": bool(live), "doing": (live or {}).get("doing"), "waiting_tasks": (live or {}).get("queued") or None,
           "notes": len(a.get("notes") or []) or None,
           "last_report": (f"{last.get('at')}: {last.get('report', '')[:300]}" if last else None)}
    return {k: v for k, v in row.items() if v not in (None, "")}


def _assign(agent_id: str, task: str | None, user_words: str | None = None) -> str:
    try:
        return _on_hub(HUB.assign(agent_id, task, source="assigned", user_words=user_words))
    except RuntimeError as exc:
        return f"ERROR: {exc}"


@tool("create_agent", "Make a bot: a named helper that works for the user BY ITSELF, in the background, while they "
      "keep chatting. It can do anything Karya can: jobs, research, markets, email, posting, PC tasks, websites. "
      "Give it a name and its job. Add every_minutes or daily_at only when it should also work on a schedule "
      "('every morning find new PM jobs and apply', 'check gold twice a day'); without one it works when it's given a "
      "task (assign_agent, or the user writes '@Name ...'). It reports in this chat and on WhatsApp, and keeps notes "
      "of what it did.", {
    "name": P("string", "Short name the user can call it by, e.g. 'Maya' or 'Job hunter'"),
    "task": P("string", "Its job, as a full request Karya can act on alone"),
    "every_minutes": P("integer", f"Also run every N minutes (at least {MIN_EVERY})"),
    "daily_at": P("array", "Also run at these times of day (24 h), e.g. ['09:00', '18:00']", items={"type": "string"}),
    "weekdays": P("array", "Only on these days: mon..sun, or 'weekdays' (default every day)", items={"type": "string"}),
}, required=["name", "task"], group="agents")
def create_agent(name: str, task: str, every_minutes: int | None = None, daily_at: list | None = None,
                 weekdays: list | None = None):
    try:
        agent = create(name, task, every_minutes, daily_at, weekdays)
    except ValueError as exc:
        return f"ERROR: {exc}"
    return {"created": _row(agent),
            "note": f"Give it work any time with assign_agent, or the user writes \"@{agent['name']} <task>\" here or on "
                    "WhatsApp. It works while Karya is running and reports here and on WhatsApp."}


@tool("assign_agent", "Give one of the user's bots a task now. It works on it in the background, in parallel with you, "
      "and reports in the chat and on WhatsApp. Use it when the user says 'ask <bot> to...', '@<bot> ...', 'let <bot> "
      "handle...', or wants something done in the background while they carry on. Don't wait for it: tell the user it's "
      "started and carry on.", {
    "agent": P("string", "The bot's name or id"),
    "task": P("string", "The task, as a full request (include everything the user said about it)"),
}, required=["agent", "task"], group="agents")
def assign_agent(agent: str, task: str):
    found = find(agent)
    if found is None:
        return f"ERROR: no bot {agent!r}. Make one with create_agent, or pick one from list_agents."
    state = _assign(found["id"], str(task or "").strip()[:2000])
    if state.startswith("ERROR"):
        return state
    return (f"{found['name']} {'started on it' if state == 'started' else 'has it queued (it finishes its current work first)'}. "
            "It reports in the chat and on WhatsApp when it's done.")


@tool("list_agents", "The user's bots: what each does, whether it's working right now (and on what), its schedule, its "
      "notes and its last report.", group="agents")
def list_agents():
    return [_row(a) for a in load()] or "No bots yet. Make one with create_agent."


@tool("update_agent", "Change one of the user's bots: pause or resume its schedule (enabled), change its job (task) or "
      "schedule, make it work only on request (on_demand), run its job now, or stop what it's doing (stop).", {
    "agent": P("string", "The bot's name or id"),
    "enabled": P("boolean", "false = pause its schedule, true = resume it"),
    "task": P("string", "Its new job"),
    "every_minutes": P("integer", "New interval in minutes"),
    "daily_at": P("array", "New times of day, e.g. ['10:00']", items={"type": "string"}),
    "weekdays": P("array", "New days, e.g. ['mon','wed'] or ['weekdays']", items={"type": "string"}),
    "on_demand": P("boolean", "true = no schedule: it works when it's given a task"),
    "run_now": P("boolean", "Do its job now"),
    "stop": P("boolean", "Stop what it's doing now (and drop its waiting tasks)"),
}, required=["agent"], group="agents")
def update_agent(agent: str, enabled: bool | None = None, task: str = "", every_minutes: int | None = None,
                 daily_at: list | None = None, weekdays: list | None = None, on_demand: bool = False,
                 run_now: bool = False, stop: bool = False):
    found = find(agent)
    if found is None:
        return f"ERROR: no bot {agent!r}"
    note = ""
    if stop:
        try:
            stopped = _on_hub(_stop(found["id"]))
        except RuntimeError as exc:
            return f"ERROR: {exc}"
        note = "Stopped it." if stopped else "It wasn't working on anything."
    try:
        row = update(found["id"], enabled=enabled, task=task, every_minutes=every_minutes, daily_at=daily_at,
                     weekdays=weekdays, on_demand=on_demand, run_now=run_now and HUB is None)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if run_now and HUB is not None:
        state = _assign(found["id"], None)
        note = (note + " " if note else "") + ("Started its job now." if state == "started" else
                                               "Its job is queued (it finishes its current work first)." if
                                               state == "queued" else state)
    return {"updated": _row(row), **({"note": note} if note else {})}


async def _stop(agent_id: str) -> bool:
    return HUB.stop_bot(agent_id)


@tool("delete_agent", "Delete one of the user's bots for good (its notes and history too).", {
    "agent": P("string", "The bot's name or id"),
}, required=["agent"], risk=CONFIRM, group="agents")
def delete_agent(agent: str):
    found = find(agent)
    if found is None:
        return f"ERROR: no bot {agent!r}"
    if HUB is not None:
        try:
            _on_hub(_stop(found["id"]))
        except RuntimeError:
            pass
    gone = delete(found["id"])
    return f"Deleted the bot {gone['name']!r}."
