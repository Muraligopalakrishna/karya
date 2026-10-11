"""Windows shortcuts (.lnk files) that make Karya open like an installed app:

- the Start menu entry: press the Windows key, type "Karya", press Enter
- an optional desktop icon
- the Startup entry, which starts Karya when the user signs in (see autostart.py)

Each one runs Karya's launcher, `pythonw.exe -m karya.desktop`: no console window; it starts Karya in the background
if it isn't running and opens its window. A shortcut is just a file: removing it changes nothing else in Windows."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT

NAME = "Karya.lnk"
DESCRIPTION = "Karya, your AI agent"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Every value reaches PowerShell as an environment variable, so no folder name can break (or inject into) the command.
_MAKE = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KARYA_LNK); $s.TargetPath = $env:KARYA_TARGET; "
         "$s.Arguments = $env:KARYA_ARGS; $s.WorkingDirectory = $env:KARYA_DIR; $s.WindowStyle = [int]$env:KARYA_STYLE; "
         "if ($env:KARYA_ICON) { $s.IconLocation = $env:KARYA_ICON }; $s.Description = $env:KARYA_DESC; $s.Save()")
_READ = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:KARYA_LNK); "
         "@{target = $s.TargetPath; args = $s.Arguments; dir = $s.WorkingDirectory; style = $s.WindowStyle; "
         "icon = $s.IconLocation} | ConvertTo-Json -Compress")
_CACHE: dict = {}


def _powershell(script: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          env={**os.environ, **env}, capture_output=True, timeout=60, creationflags=_NO_WINDOW)


def make(path: Path, target: str, args: str = "", workdir: str = "", icon: str = "", description: str = DESCRIPTION,
         style: int = 1) -> None:
    """Write one shortcut. style: 1 = normal window, 7 = minimized."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    r = _powershell(_MAKE, KARYA_LNK=str(path), KARYA_TARGET=str(target), KARYA_ARGS=str(args), KARYA_DIR=str(workdir),
                    KARYA_ICON=str(icon), KARYA_DESC=str(description), KARYA_STYLE=str(int(style)))
    if r.returncode != 0 or not path.exists():
        lines = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
        raise ValueError("Windows didn't make the shortcut" + (f": {lines[-1][:200]}" if lines else "."))
    _CACHE.pop(str(path), None)


def read(path: Path) -> dict:
    """What a shortcut runs: {target, args, dir, style, icon}, or {} when there's none. Read once per change of the
    file (reading takes PowerShell)."""
    path = Path(path)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = _CACHE.get(str(path))
    if cached and cached[0] == stamp:
        return dict(cached[1])
    r = _powershell(_READ, KARYA_LNK=str(path))
    try:
        data = json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    _CACHE[str(path)] = (stamp, data)
    return dict(data)


def start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path:
    """The user's real desktop folder (it may live in OneDrive)."""
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:  # CSIDL_DESKTOPDIRECTORY
            return Path(buf.value)
    return Path.home() / "Desktop"


PLACES = ("start_menu", "desktop")


def path_for(where: str) -> Path:
    # the folder functions are looked up on each call (tests point them at a temporary folder)
    if where == "start_menu":
        return start_menu_dir() / NAME
    if where == "desktop":
        return desktop_dir() / NAME
    raise ValueError(f"unknown place {where!r}")


def launcher(root: Path = ROOT, background: bool = False) -> tuple[str, str]:
    """(target, arguments) that start this copy of Karya as an app."""
    root = Path(root)
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    if pythonw.exists():
        return str(pythonw), "-m karya.desktop" + (" --background" if background else "")
    return str(root / "start.bat"), "--no-browser" if background else ""


def icon(root: Path = ROOT) -> str:
    ico = Path(root) / "karya" / "static" / "karya.ico"
    return f"{ico},0" if ico.exists() else ""


def points_here(path: Path, root: Path = ROOT) -> bool:
    """The shortcut starts THIS copy of Karya (the launcher, or start.bat in older versions)."""
    target = str(read(path).get("target") or "").lower()
    root = Path(root)
    return bool(target) and target in {str(root / ".venv" / "Scripts" / "pythonw.exe").lower(),
                                       str(root / "start.bat").lower()}


def app_status(root: Path = ROOT) -> dict:
    supported = sys.platform == "win32"
    out = {"supported": supported}
    for where in PLACES:
        path = path_for(where) if supported else None
        out[where] = bool(path and path.exists() and points_here(path, root))
    return out


def add(where: str, root: Path = ROOT) -> None:
    if sys.platform != "win32":
        raise ValueError("Shortcuts work on Windows only.")
    root = Path(root)
    if not (root / "start.bat").exists():
        raise ValueError(f"Karya isn't in {root}")
    target, args = launcher(root)
    make(path_for(where), target, args, str(root), icon(root))


def remove(where: str, root: Path = ROOT) -> None:
    """Remove the shortcut if it's this Karya's (a shortcut to another copy of Karya is left alone)."""
    path = path_for(where)
    if path.exists() and points_here(path, root):
        path.unlink()
        _CACHE.pop(str(path), None)


def set_place(where: str, on: bool, root: Path = ROOT) -> dict:
    add(where, root) if on else remove(where, root)
    if where == "start_menu":
        from .config import update_env
        update_env({"KARYA_START_MENU": "true" if on else "false"})
    return app_status(root)


def first_run(root: Path = ROOT) -> bool:
    """The first time Karya starts, it adds itself to the Start menu, as an installer would. Never again once the
    user has turned that off (KARYA_START_MENU=false). True if it made the entry."""
    from .config import _env, update_env
    if sys.platform != "win32" or _env("KARYA_START_MENU"):
        return False
    path = path_for("start_menu")
    made = False
    if not path.exists() or not Path(str(read(path).get("target") or "")).exists():
        add("start_menu", root)
        made = True
    update_env({"KARYA_START_MENU": "true"})
    return made
