"""Background agents: tasks Karya runs by itself on a schedule and reports on.

The user, 2026-10-10: "build agents and launch them ... like Grok bots". Each agent is a saved task with a schedule
("every morning find new PM jobs and apply", "twice a day check gold and tell me"). When one is due and Karya is
free, it runs through the same agent loop, approvals and checks as a chat request. The report goes to the chat and,
when it's linked, to the user's WhatsApp, where approvals are asked too (refused after UNATTENDED_MINUTES without an
answer). Agents run while Karya, and so the PC, is on."""
from __future__ import annotations

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
MIN_EVERY = 15                 # minutes: no agent runs more often than this
UNATTENDED_MINUTES = 30        # an approval nobody answers is refused after this long
_LOCK = threading.RLock()


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


def find(key: str) -> dict | None:
    key = str(key or "").strip().lower()
    agents = load()
    return next((a for a in agents if a["id"].lower() == key), None) or \
        next((a for a in agents if a["name"].lower() == key), None) or \
        next((a for a in agents if key and key in a["name"].lower()), None)


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


def next_run(agent: dict, after: datetime | None = None) -> float:
    """When the agent is due next (epoch seconds)."""
    after = after or datetime.now()
    days = agent.get("weekdays") or list(WEEKDAYS)
    if agent.get("every_minutes"):
        step = timedelta(minutes=max(MIN_EVERY, int(agent["every_minutes"])))
        last = agent.get("last_run")
        when = datetime.fromtimestamp(last) + step if last else after + timedelta(minutes=1)
        while when < after:
            when += step
        for _ in range(8):                  # skip days the agent doesn't run on
            if WEEKDAYS[when.weekday()] in days:
                break
            when = (when + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        return when.timestamp()
    best = None
    for hhmm in agent.get("daily_at") or ["09:00"]:
        hour, minute = (int(x) for x in hhmm.split(":"))
        for offset in range(8):
            day = after + timedelta(days=offset)
            when = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when > after and WEEKDAYS[when.weekday()] in days:
                best = when if best is None or when < best else best
                break
    return (best or after + timedelta(days=1)).timestamp()


def describe(agent: dict) -> str:
    days = agent.get("weekdays") or []
    on = "" if not days or len(days) == 7 else " on " + ", ".join(d.title() for d in days)
    if agent.get("every_minutes"):
        every = int(agent["every_minutes"])
        return (f"every {every // 60} h" if every % 60 == 0 else f"every {every} min") + on
    return "daily at " + ", ".join(agent.get("daily_at") or ["09:00"]) + on


# ---------------------------------------------------------------- changes
def create(name: str, task: str, every_minutes: int | None = None, daily_at=None, weekdays=None) -> dict:
    name, task = str(name or "").strip()[:60], str(task or "").strip()[:2000]
    if not name or not task:
        raise ValueError("give the agent a name and a task")
    times = [t for t in (clock(x) for x in (daily_at or [])) if t]
    if daily_at and not times:
        raise ValueError("daily_at must be times like '09:00' or '6:30 pm'")
    if not every_minutes and not times:
        times = ["09:00"]
    agent = {"id": "a" + uuid.uuid4().hex[:6], "name": name, "task": task,
             "every_minutes": max(MIN_EVERY, int(every_minutes)) if every_minutes and not times else None,
             "daily_at": times or None, "weekdays": _days(weekdays) or None, "enabled": True,
             "created": time.time(), "last_run": None, "runs": []}
    agent["next_run"] = next_run(agent)
    with _LOCK:
        agents = load()
        if any(a["name"].lower() == name.lower() for a in agents):
            raise ValueError(f"there's already an agent called {name!r}: change it with update_agent")
        agents.append(agent)
        _save(agents)
    return agent


def update(key: str, **fields) -> dict:
    with _LOCK:
        agents = load()
        agent = next((a for a in agents if a["id"] == (find(key) or {}).get("id")), None)
        if agent is None:
            raise ValueError(f"no agent {key!r}")
        if fields.get("task"):
            agent["task"] = str(fields["task"]).strip()[:2000]
        if fields.get("name"):
            agent["name"] = str(fields["name"]).strip()[:60]
        if fields.get("enabled") is not None:
            agent["enabled"] = bool(fields["enabled"])
        if fields.get("every_minutes"):
            agent["every_minutes"], agent["daily_at"] = max(MIN_EVERY, int(fields["every_minutes"])), None
        if fields.get("daily_at"):
            times = [t for t in (clock(x) for x in fields["daily_at"]) if t]
            if not times:
                raise ValueError("daily_at must be times like '09:00'")
            agent["daily_at"], agent["every_minutes"] = times, None
        if fields.get("weekdays") is not None:
            agent["weekdays"] = _days(fields["weekdays"]) or None
        if fields.get("run_now"):
            agent["next_run"] = time.time()
        elif any(fields.get(k) is not None for k in ("every_minutes", "daily_at", "weekdays", "enabled")):
            agent["next_run"] = next_run(agent)
        _save(agents)
        return agent


def delete(key: str) -> dict:
    with _LOCK:
        agents = load()
        agent = find(key)
        if agent is None:
            raise ValueError(f"no agent {key!r}")
        _save([a for a in agents if a["id"] != agent["id"]])
        return agent


def due(now: float | None = None) -> list[dict]:
    now = now or time.time()
    return sorted((a for a in load() if a.get("enabled") and (a.get("next_run") or 0) <= now),
                  key=lambda a: a.get("next_run") or 0)


def started(agent_id: str) -> None:
    """A run began: it won't be picked again until its next time."""
    with _LOCK:
        agents = load()
        for a in agents:
            if a["id"] == agent_id:
                a["last_run"] = time.time()
                a["next_run"] = next_run(a, datetime.now() + timedelta(seconds=30))
        _save(agents)


def finished(agent_id: str, report: str) -> None:
    with _LOCK:
        agents = load()
        for a in agents:
            if a["id"] == agent_id:
                a.setdefault("runs", []).append({"at": time.strftime("%Y-%m-%d %H:%M"), "report": str(report or "")[:600]})
                del a["runs"][:-10]
        _save(agents)


def run_text(agent: dict) -> str:
    return (f"[Background agent \"{agent['name']}\", scheduled run at {time.strftime('%H:%M')}] {agent['task']}\n"
            "Work on your own, as far as you can without the user. End with a short report for them: what you did, "
            "what you found, and anything that needs them.")


class Hooks:
    """Hub listener: records each agent run's report."""

    async def on_confirm(self, request: dict) -> None:
        return None

    async def on_ask(self, request: dict) -> None:
        return None

    async def on_done(self, source: str, text: str, final: str) -> None:
        if source.startswith("agent:"):
            finished(source.split(":", 1)[1], final)


async def loop(hub, every: float = 20.0) -> None:
    """Start due agents, one at a time, whenever Karya is free."""
    import asyncio
    while True:
        await asyncio.sleep(every)
        try:
            if hub.busy_now():
                continue
            if hub.queue:                      # tasks from WhatsApp that waited for an MCP call to end
                await hub.next_queued()
                continue
            for agent in due():
                started(agent["id"])
                await hub.submit(run_text(agent), source=f"agent:{agent['id']}", label=agent["name"])
                break
        except Exception:  # noqa: BLE001 - a broken agent file never stops the server
            continue


# ---------------------------------------------------------------- tools
def _row(a: dict) -> dict:
    nxt = datetime.fromtimestamp(a["next_run"]).strftime("%a %d %b %H:%M") if a.get("next_run") else ""
    last = (a.get("runs") or [{}])[-1]
    return {k: v for k, v in {"id": a["id"], "name": a["name"], "task": a["task"][:200], "when": describe(a),
                              "enabled": a.get("enabled"), "next_run": nxt if a.get("enabled") else "paused",
                              "last_report": (f"{last.get('at')}: {last.get('report', '')[:200]}" if last else None)}.items()
            if v not in (None, "")}


@tool("create_agent", "Make a background agent: a task Karya runs BY ITSELF on a schedule and reports on (in this chat "
      "and on the user's WhatsApp when linked). Use it whenever the user wants something done regularly or watched: "
      "'every morning find new PM jobs and apply', 'check gold twice a day and tell me', 'every Friday post my reel'. "
      "Give every_minutes OR daily_at (default daily at 09:00). The task must be a full request Karya can act on alone.", {
    "name": P("string", "Short name, e.g. 'Job hunter'"),
    "task": P("string", "What to do each time, as a full request to Karya"),
    "every_minutes": P("integer", f"Run every N minutes (at least {MIN_EVERY})"),
    "daily_at": P("array", "Times of day (24 h), e.g. ['09:00', '18:00']", items={"type": "string"}),
    "weekdays": P("array", "Only on these days: mon..sun, or 'weekdays' (default every day)", items={"type": "string"}),
}, required=["name", "task"], group="agents")
def create_agent(name: str, task: str, every_minutes: int | None = None, daily_at: list | None = None,
                 weekdays: list | None = None):
    try:
        agent = create(name, task, every_minutes, daily_at, weekdays)
    except ValueError as exc:
        return f"ERROR: {exc}"
    return {"created": _row(agent), "note": "It runs while Karya is on. Reports come here (and on WhatsApp when linked)."}


@tool("list_agents", "The user's background agents: what each does, when it runs next and its last report.", group="agents")
def list_agents():
    return [_row(a) for a in load()] or "No background agents yet. Make one with create_agent."


@tool("update_agent", "Change a background agent: pause or resume it (enabled), its task, its schedule, or run it now.", {
    "agent": P("string", "The agent's id or name"),
    "enabled": P("boolean", "false = pause, true = resume"),
    "task": P("string", "New task"),
    "every_minutes": P("integer", "New interval in minutes"),
    "daily_at": P("array", "New times of day, e.g. ['10:00']", items={"type": "string"}),
    "weekdays": P("array", "New days, e.g. ['mon','wed'] or ['weekdays']", items={"type": "string"}),
    "run_now": P("boolean", "Run it as soon as Karya is free"),
}, required=["agent"], group="agents")
def update_agent(agent: str, enabled: bool | None = None, task: str = "", every_minutes: int | None = None,
                 daily_at: list | None = None, weekdays: list | None = None, run_now: bool = False):
    try:
        return {"updated": _row(update(agent, enabled=enabled, task=task, every_minutes=every_minutes,
                                       daily_at=daily_at, weekdays=weekdays, run_now=run_now))}
    except ValueError as exc:
        return f"ERROR: {exc}"


@tool("delete_agent", "Delete a background agent for good.", {
    "agent": P("string", "The agent's id or name"),
}, required=["agent"], risk=CONFIRM, group="agents")
def delete_agent(agent: str):
    try:
        gone = delete(agent)
    except ValueError as exc:
        return f"ERROR: {exc}"
    return f"Deleted the agent {gone['name']!r}."
