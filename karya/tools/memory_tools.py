"""Long-term memory tools."""
from __future__ import annotations

from ..memory import memory_store, now
from ..registry import P, tool


@tool("remember", "Save a lasting fact/preference about the user (e.g. 'prefers remote jobs', 'LinkedIn: ...').", {
    "text": P("string", "The fact to remember"),
}, required=["text"], group="memory")
def remember(text: str):
    data = memory_store.load()
    notes = data.setdefault("notes", [])
    note_id = max([n.get("id", 0) for n in notes] + [0]) + 1
    notes.append({"id": note_id, "text": text.strip(), "created": now()})
    memory_store.save(data)
    return f"Remembered (#{note_id})."


@tool("recall", "Search remembered notes and profile.", {
    "query": P("string", "Keyword(s); empty returns everything"),
}, group="memory")
def recall(query: str = ""):
    data = memory_store.load()
    words = [w.lower() for w in query.split() if w.strip()]
    notes = [n for n in data.get("notes", []) if not words or any(w in n["text"].lower() for w in words)]
    return {"profile": data.get("profile", {}), "notes": notes}


@tool("forget", "Delete a remembered note by id.", {"note_id": P("integer", "Note id")}, required=["note_id"], group="memory")
def forget(note_id: int):
    data = memory_store.load()
    before = len(data.get("notes", []))
    data["notes"] = [n for n in data.get("notes", []) if n.get("id") != note_id]
    memory_store.save(data)
    return "Forgotten." if len(data["notes"]) < before else f"ERROR: no note {note_id}"


@tool("update_profile", "Set a field in the user's profile (name, email, phone, city, skills, job_preferences, links...).", {
    "field": P("string", "Profile field name"),
    "value": P("string", "New value (empty to remove)"),
}, required=["field"], group="memory")
def update_profile(field: str, value: str = ""):
    from .. import answers
    if value and field in answers.SENSITIVE_FIELDS and not answers.said_by_user(value):
        return (f"ERROR: {field} has to be the user's own answer, and they didn't say \"{value}\". Ask them with "
                "ask_user (it saves their answer) instead of saving a guess.")
    data = memory_store.load()
    profile = data.setdefault("profile", {})
    if value:
        profile[field] = value
    else:
        profile.pop(field, None)
    memory_store.save(data)
    return f"Profile updated: {field}"



@tool("karya_activity", "What Karya really did (from its own logs): emails sent, posts made, job applications, and how "
      "many are left under today's limits. Check it before telling the user something was sent or posted.", {
    "days": P("integer", "How many days back (default 1 = today)"),
}, group="memory")
def karya_activity(days: int = 1):
    import time as _time
    from .. import outbox
    from ..config import settings
    from ..memory import applications_store
    days = max(1, min(int(days or 1), 30))
    since = _time.strftime("%Y-%m-%d", _time.localtime(_time.time() - (days - 1) * 86400))
    data = outbox._load()
    emails = [{"to": r["to"], "subject": r.get("subject", ""), "time": r.get("time", ""), "status": r.get("status")}
              for r in data.get("sent", []) if r.get("time", "")[:10] >= since]
    posts = [{"site": p.get("site"), "time": p.get("time"), "text": (p.get("text") or "")[:80]}
             for p in data.get("posts", []) if p.get("time", "")[:10] >= since]
    apps = [{"company": a.get("company"), "role": a.get("role"), "status": a.get("status"), "time": a.get("created")}
            for a in applications_store.load().get("applications", []) if str(a.get("created", ""))[:10] >= since]
    from .jobs import applications_today
    return {"since": since, "emails": emails[-60:], "posts": posts[-30:], "applications": apps[-60:],
            "bounced": data.get("bounced", [])[-20:],
            "left_today": {"emails": max(0, settings.max_emails_per_day - outbox.sent_today()),
                           "posts": max(0, settings.max_posts_per_day - outbox.posts_today()),
                           "applications": max(0, settings.max_applications_per_day - applications_today())}}


@tool("task_status", "What the user asked for in the current task, every follow-up they gave, and what's already done. "
      "Check it when you're unsure what to do next.", group="core")
def task_status():
    from .. import focus
    return focus.CURRENT.get("block") or "No task in progress."


@tool("enable_tools", "Load more tool groups when you need a capability you don't see: "
      "finance, jobs, email, browser, pc, files, website, memory, agents.", {
    "groups": P("array", "Groups to load", items={"type": "string",
                                                  "enum": ["finance", "jobs", "email", "browser", "pc", "files", "website", "memory", "resume", "accounts", "agents", "web"]}),
}, required=["groups"], group="core")
def enable_tools(groups: list[str]):
    return f"Loaded tool groups: {', '.join(groups)}. They are available from your next step."
