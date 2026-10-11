"""The first-run guide: what someone sees right after downloading Karya.

Three short steps, each skippable: 1. connect an AI (the only one Karya can't work without), 2. about you (name,
email, city, and their resume file, which Karya turns into its master resume), 3. extras (WhatsApp, the Chrome
extension, start with Windows: all optional). The guide shows again until the user finishes or skips it
(KARYA_ONBOARDED in .env), and Setup > More can show it again."""
from __future__ import annotations

import base64
import re
import threading
from pathlib import Path

from .config import DATA_DIR, _env, settings, update_env

RESUME_DIR = DATA_DIR / "resume"
MAX_RESUME_BYTES = 8 * 1024 * 1024
RESUME_TYPES = (".pdf", ".docx", ".txt", ".md")
FIELDS = {"name": 80, "email": 120, "location": 120, "phone": 30, "target_roles": 200}
IMPORT: dict = {"state": "idle", "error": ""}


def _profile() -> dict:
    from .memory import memory_store
    return memory_store.load().get("profile", {})


def state() -> dict:
    """Which steps are done, so the guide opens at the first one that isn't."""
    from .tools import resume as resume_tools
    profile = _profile()
    ai = [p.name for p in settings.providers if p.name != "ollama"]
    from . import local_ai
    offline_ready = any(p.name == "ollama" for p in settings.providers) and local_ai.status(cached=True).get("ready")
    master = resume_tools.load_master()
    return {
        "done": _env("KARYA_ONBOARDED", "").lower() in ("1", "true", "yes", "skipped"),
        "ai": bool(ai) or bool(offline_ready),
        "ai_names": ai,
        "about": bool(profile.get("name")),
        "profile": {k: str(profile.get(k) or "")[:FIELDS[k]] for k in FIELDS if profile.get(k)},
        "resume": bool(master) or settings.resume_path.exists(),
        "resume_file": settings.resume_path.name if settings.resume_path.exists() else "",
        "resume_import": dict(IMPORT),
    }


def save_profile(values: dict) -> dict:
    """Only the fields the guide asks for, trimmed; the user typed them, so they count as their own words."""
    from . import answers
    from .memory import memory_store
    data = memory_store.load()
    profile = data.setdefault("profile", {})
    for key, limit in FIELDS.items():
        value = re.sub(r"\s+", " ", str(values.get(key) or "")).strip()[:limit]
        if not value:
            continue
        if key == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("That email address doesn't look right.")
        profile[key] = value
        answers.note_user_message(value)
    memory_store.save(data)
    return state()


def save_resume(filename: str, b64: str) -> dict:
    """The resume file from the page: saved under data/resume, set as the resume in Settings, then turned into
    Karya's master resume in the background (with the user's AI)."""
    name = Path(str(filename or "")).name
    suffix = Path(name).suffix.lower()
    if suffix not in RESUME_TYPES:
        raise ValueError("Choose a PDF, Word (.docx) or text file.")
    try:
        raw = base64.b64decode(str(b64 or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("The file didn't come through. Try again.") from exc
    if not raw or len(raw) > MAX_RESUME_BYTES:
        raise ValueError("The file is empty or bigger than 8 MB.")
    if suffix == ".pdf" and not raw.startswith(b"%PDF"):
        raise ValueError("That file isn't a PDF.")
    if suffix == ".docx" and not raw.startswith(b"PK"):
        raise ValueError("That file isn't a Word document.")
    RESUME_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\- ]+", "_", Path(name).stem)[:60] or "resume"
    path = RESUME_DIR / f"{safe}{suffix}"
    path.write_bytes(raw)
    update_env({"RESUME_PATH": str(path)})
    _import_in_background(path)
    return state()


def _import_in_background(path: Path) -> None:
    from .tools import jobs
    jobs._resume_cache.pop(str(path), None)
    IMPORT.update(state="reading", error="")

    def work():
        from .registry import run_tool
        from .tools import resume as resume_tools
        try:
            if not resume_tools.own_ai():
                IMPORT.update(state="later", error="Karya reads it as soon as an AI is connected.")
                return
            out = run_tool("import_resume", {"path": str(path)})
            if out.startswith("ERROR") or '"resume_text"' in out or not resume_tools.load_master():
                IMPORT.update(state="failed", error=out[:160] if out.startswith("ERROR") else
                              "Karya couldn't read it. Ask Karya in the chat: \"import my resume\".")
            else:
                IMPORT.update(state="done", error="")
        except Exception as exc:  # noqa: BLE001 - shown in the guide
            IMPORT.update(state="failed", error=str(exc)[:160])
    threading.Thread(target=work, daemon=True, name="karya-resume-import").start()


def import_if_waiting() -> None:
    """An AI was just connected: read the resume the user gave earlier (once)."""
    from .tools import resume as resume_tools
    if IMPORT.get("state") == "later" or (settings.resume_path.exists() and not resume_tools.load_master()
                                           and IMPORT.get("state") == "idle"):
        if settings.resume_path.exists():
            _import_in_background(settings.resume_path)


def finish(skipped: bool = False) -> dict:
    update_env({"KARYA_ONBOARDED": "skipped" if skipped else "true"})
    return state()


def reset() -> dict:
    update_env({"KARYA_ONBOARDED": ""})
    return state()
