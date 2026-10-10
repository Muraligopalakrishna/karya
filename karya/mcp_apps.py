"""One-click "Add Karya" for other AI apps and CLIs (MCP): finds the ones installed on this PC, shows which already
have Karya, and adds it. JSON configs are merged (your other servers stay; a backup of the old file is kept next to
it); Claude Code is added through its own CLI; Codex gets a [mcp_servers.karya] block with time to approve."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import ROOT

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming")


def server_entry(root: Path = ROOT) -> dict:
    return {"command": str(root / ".venv" / "Scripts" / "python.exe"), "args": [str(root / "karya_mcp.py")]}


def _claude_desktop_config() -> Path:
    """The Microsoft Store build keeps its config in a package folder."""
    packages = Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local") / "Packages"
    for pkg in sorted(packages.glob("Claude_*")) if packages.exists() else []:
        candidate = pkg / "LocalCache" / "Roaming" / "Claude"
        if candidate.exists():
            return candidate / "claude_desktop_config.json"
    return _appdata() / "Claude" / "claude_desktop_config.json"


def apps() -> list[dict]:
    """Every app Karya knows how to add itself to: where its config lives and whether it's installed."""
    home = _home()
    rows = [
        {"id": "claude_code", "name": "Claude Code", "kind": "claude_cli", "file": home / ".claude.json",
         "installed": bool(shutil.which("claude"))},
        {"id": "codex", "name": "Codex CLI and app", "kind": "toml", "file": home / ".codex" / "config.toml",
         "installed": bool(shutil.which("codex")) or (home / ".codex").exists()},
        {"id": "kiro", "name": "Kiro (IDE and CLI)", "kind": "json", "key": "mcpServers",
         "file": home / ".kiro" / "settings" / "mcp.json",
         "installed": bool(shutil.which("kiro-cli")) or (home / ".kiro").exists()},
        {"id": "cursor", "name": "Cursor", "kind": "json", "key": "mcpServers", "file": home / ".cursor" / "mcp.json",
         "installed": (home / ".cursor").exists()},
        {"id": "claude_desktop", "name": "Claude Desktop", "kind": "json", "key": "mcpServers",
         "file": _claude_desktop_config(), "installed": _claude_desktop_config().parent.exists()},
        {"id": "vscode", "name": "VS Code", "kind": "json", "key": "servers", "extra": {"type": "stdio"},
         "file": _appdata() / "Code" / "User" / "mcp.json", "installed": (_appdata() / "Code" / "User").exists()},
        {"id": "gemini", "name": "Gemini CLI", "kind": "json", "key": "mcpServers", "extra": {"timeout": 900000},
         "file": home / ".gemini" / "settings.json",
         "installed": bool(shutil.which("gemini")) or (home / ".gemini").exists()},
        {"id": "windsurf", "name": "Windsurf", "kind": "json", "key": "mcpServers",
         "file": home / ".codeium" / "windsurf" / "mcp_config.json",
         "installed": (home / ".codeium" / "windsurf").exists()},
        {"id": "qwen", "name": "Qwen Code", "kind": "json", "key": "mcpServers", "extra": {"timeout": 900000},
         "file": home / ".qwen" / "settings.json", "installed": bool(shutil.which("qwen")) or (home / ".qwen").exists()},
    ]
    return rows


def _read_json(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
        return {}
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("the file isn't a JSON object")
    return data


def _state(app: dict, root: Path = ROOT) -> dict:
    """connected (this Karya), other (Karya from another folder), no, or unreadable."""
    want = server_entry(root)["command"].lower()
    path: Path = app["file"]
    try:
        if app["kind"] == "toml":
            text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            block = re.search(r"(?ms)^\[mcp_servers\.karya\]\s*$(.*?)(?=^\[|\Z)", text)
            if not block:
                return {"state": "no"}
            return {"state": "connected" if want in block.group(1).replace("\\\\", "\\").lower() else "other"}
        data = _read_json(path)
        servers = data.get("mcpServers" if app["kind"] == "claude_cli" else app["key"]) or {}
        entry = servers.get("karya") if isinstance(servers, dict) else None
        if not entry:
            return {"state": "no"}
        return {"state": "connected" if str(entry.get("command", "")).lower() == want else "other"}
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def overview(root: Path = ROOT) -> list[dict]:
    return [{"id": a["id"], "name": a["name"], "installed": a["installed"], "file": str(a["file"]), **_state(a, root)}
            for a in apps()]


def connect(app_id: str, root: Path = ROOT) -> dict:
    """Add (or point to this folder) Karya in one app's MCP config. Returns the new state and what to do next."""
    app = next((a for a in apps() if a["id"] == app_id), None)
    if app is None:
        raise ValueError(f"unknown app {app_id!r}")
    entry = server_entry(root)
    path: Path = app["file"]
    if app["kind"] == "claude_cli":
        cli = shutil.which("claude")
        if not cli:
            raise ValueError("Claude Code isn't installed (the claude command wasn't found).")
        run = lambda *args: subprocess.run([cli, "mcp", *args], capture_output=True, timeout=60,  # noqa: E731
                                           creationflags=_NO_WINDOW)
        run("remove", "--scope", "user", "karya")      # an older entry (another folder) is replaced
        r = run("add", "--scope", "user", "karya", "--", entry["command"], *entry["args"])
        if r.returncode != 0:
            raise ValueError("Claude Code refused: " + (r.stderr or r.stdout).decode("utf-8", "replace").strip()[:200])
    elif app["kind"] == "toml":
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        toml_path = lambda p: p.replace("\\", "\\\\")  # noqa: E731
        block = (f'[mcp_servers.karya]\ncommand = "{toml_path(entry["command"])}"\n'
                 f'args = ["{toml_path(entry["args"][0])}"]\n'
                 "tool_timeout_sec = 900   # time for you to approve sends and posts\n")
        _backup(path)
        text = re.sub(r"(?ms)^\[mcp_servers\.karya\]\s*$.*?(?=^\[|\Z)", "", text).rstrip()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((text + "\n\n" if text else "") + block, encoding="utf-8")
    else:
        data = _read_json(path)                         # unreadable (comments, broken JSON): left untouched
        servers = data.get(app["key"])
        if servers is None:
            servers = data[app["key"]] = {}
        if not isinstance(servers, dict):
            raise ValueError(f"{path.name} has an unexpected '{app['key']}' section; add Karya by hand")
        servers["karya"] = {**app.get("extra", {}), **entry}
        _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"id": app_id, "name": app["name"], **_state(app, root),
            "next": f"Restart {app['name']} so it loads Karya's tools."}


def _backup(path: Path) -> None:
    if path.exists():
        backup = path.with_name(path.name + ".before-karya")
        if not backup.exists():                         # keep the very first version
            shutil.copy2(path, backup)
