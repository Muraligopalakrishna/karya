"""Setup > "Start Karya when I sign in to Windows".

Karya reads WhatsApp and runs bots only while it's running on the PC. With this on, Windows starts it by itself when
the user signs in: a shortcut to start.bat --no-browser in their Startup folder, minimized. Then a WhatsApp message
works whenever the PC is on, without opening Karya first. Turning it off deletes the shortcut; nothing else in
Windows changes. It shows in Task Manager > Startup apps."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT
from .registry import CONFIRM, P, tool

NAME = "Karya.lnk"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The paths reach PowerShell as environment variables, so no folder name can break (or inject into) the command.
_MAKE = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KARYA_LNK); $s.TargetPath = $env:KARYA_BAT; "
         "$s.Arguments = '--no-browser'; $s.WorkingDirectory = $env:KARYA_DIR; $s.WindowStyle = 7; "
         "$s.Description = 'Starts Karya when you sign in, so WhatsApp tasks and bots work'; $s.Save()")
_READ = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KARYA_LNK); "
         "@{target = $s.TargetPath; args = $s.Arguments; dir = $s.WorkingDirectory; style = $s.WindowStyle} | "
         "ConvertTo-Json -Compress")


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / NAME


def _powershell(script: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          env={**os.environ, **env}, capture_output=True, timeout=60, creationflags=_NO_WINDOW)


_TARGET: dict = {"key": None, "target": ""}


def _target() -> str:
    """Where the startup shortcut points (read once per change of the file: reading it takes PowerShell)."""
    path = shortcut_path()
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return ""
    if _TARGET["key"] != key:
        _TARGET.update(key=key, target=str(read_shortcut().get("target") or ""))
    return _TARGET["target"]


def status(root: Path = ROOT) -> dict:
    """on: Windows starts THIS copy of Karya (a shortcut to another folder's Karya doesn't count)."""
    supported = sys.platform == "win32"
    if not supported or not shortcut_path().exists():
        return {"supported": supported, "on": False}
    return {"supported": True, "on": _target().lower() == str(Path(root) / "start.bat").lower()}


def read_shortcut() -> dict:
    """What the startup shortcut runs ({} when there's none)."""
    if not shortcut_path().exists():
        return {}
    r = _powershell(_READ, KARYA_LNK=str(shortcut_path()))
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def enable(root: Path = ROOT) -> dict:
    if sys.platform != "win32":
        raise ValueError("Starting with Windows works on Windows only.")
    root = Path(root)
    bat = root / "start.bat"
    if not bat.exists():
        raise ValueError(f"start.bat isn't in {root}")
    startup_dir().mkdir(parents=True, exist_ok=True)
    r = _powershell(_MAKE, KARYA_LNK=str(shortcut_path()), KARYA_BAT=str(bat), KARYA_DIR=str(root))
    if r.returncode != 0 or not shortcut_path().exists():
        lines = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
        raise ValueError("Windows didn't make the startup shortcut" + (f": {lines[-1][:200]}" if lines else "."))
    _TARGET["key"] = None
    return status(root)


def disable() -> dict:
    try:
        shortcut_path().unlink(missing_ok=True)
    except OSError as exc:
        raise ValueError(f"couldn't remove the startup shortcut: {exc}") from exc
    return status()


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
        return ("Karya now starts by itself when you sign in to Windows (minimized in the taskbar; it shows in Task "
                "Manager > Startup apps).")
    return "Karya won't start with Windows any more."
