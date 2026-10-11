"""Setup > "Start Karya when I sign in to Windows".

Karya reads WhatsApp and runs bots only while it's running on the PC. With this on, Windows starts it by itself when
the user signs in: a shortcut in their Startup folder to Karya's launcher (`pythonw.exe -m karya.desktop
--background`), which starts Karya in the background without a window. Then a WhatsApp message works whenever the PC
is on, without opening Karya first. Turning it off deletes the shortcut; nothing else in Windows changes. It shows in
Task Manager > Startup apps. (Older versions pointed the shortcut at start.bat; those still count and are upgraded.)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import shortcuts
from .config import ROOT
from .registry import CONFIRM, P, tool

NAME = shortcuts.NAME


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / NAME


def read_shortcut() -> dict:
    """What the startup shortcut runs ({} when there's none)."""
    return shortcuts.read(shortcut_path())


def status(root: Path = ROOT) -> dict:
    """on: Windows starts THIS copy of Karya (a shortcut to another folder's Karya doesn't count)."""
    supported = sys.platform == "win32"
    if not supported or not shortcut_path().exists():
        return {"supported": supported, "on": False}
    return {"supported": True, "on": shortcuts.points_here(shortcut_path(), root)}


def enable(root: Path = ROOT) -> dict:
    if sys.platform != "win32":
        raise ValueError("Starting with Windows works on Windows only.")
    root = Path(root)
    if not (root / "start.bat").exists():
        raise ValueError(f"start.bat isn't in {root}")
    target, args = shortcuts.launcher(root, background=True)
    shortcuts.make(shortcut_path(), target, args, str(root), shortcuts.icon(root),
                   "Starts Karya when you sign in, so WhatsApp tasks and bots work", style=7)
    return status(root)


def disable() -> dict:
    try:
        shortcut_path().unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"couldn't remove the startup shortcut: {exc}") from exc
    return status()


def upgrade(root: Path = ROOT) -> bool:
    """An older startup shortcut (start.bat, a console window in the taskbar) becomes the hidden launcher."""
    root = Path(root)
    if not status(root)["on"] or not (root / ".venv" / "Scripts" / "pythonw.exe").exists():
        return False
    if str(read_shortcut().get("target") or "").lower() != str(root / "start.bat").lower():
        return False
    enable(root)
    return True


@tool("start_with_windows", "Turn on (or off) starting Karya by itself when the user signs in to Windows, so WhatsApp "
      "tasks and scheduled bots work whenever the PC is on, without opening Karya first.", {
    "on": P("boolean", "true = start with Windows, false = don't"),
}, required=["on"], risk=CONFIRM, group="agents")
def start_with_windows(on: bool = True):
    try:
        state = enable() if on else disable()
    except ValueError as exc:
        return f"ERROR: {exc}"
    if state["on"]:
        return ("Karya now starts by itself when you sign in to Windows (in the background; it shows in Task "
                "Manager > Startup apps).")
    return "Karya won't start with Windows any more."
