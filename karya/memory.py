"""Small JSON-backed stores: user profile, remembered facts, job applications."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import DATA_DIR

_LOCK = threading.RLock()


class JsonStore:
    def __init__(self, path: Path, default: dict):
        self.path = path
        self.default = default

    def load(self) -> dict:
        with _LOCK:
            if not self.path.exists():
                return json.loads(json.dumps(self.default))
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
                return json.loads(json.dumps(self.default))

    def save(self, data: dict) -> None:
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)


memory_store = JsonStore(DATA_DIR / "memory.json", {"profile": {}, "notes": []})
applications_store = JsonStore(DATA_DIR / "applications.json", {"applications": []})


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def profile_text(max_chars: int = 1800) -> str:
    data = memory_store.load()
    lines = []
    for key, value in data.get("profile", {}).items():
        if key == "screening_answers":
            if isinstance(value, dict) and value:
                lines.append(f"- saved form answers: {len(value)} (get_application_profile shows them)")
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"- {key}: {value}")
    notes = data.get("notes", [])[-15:]
    if notes:
        lines.append("Remembered notes:")
        lines.extend(f"- [{n['id']}] {n['text']}" for n in notes)
    text = "\n".join(lines)
    return text[:max_chars] if text else "(no profile saved yet)"
