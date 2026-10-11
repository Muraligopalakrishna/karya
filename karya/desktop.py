"""Karya as an app on your PC.

    pythonw -m karya.desktop                start Karya if it isn't running (in the background, no console window),
                                            then open its window, or bring it to the front if it's already open
    pythonw -m karya.desktop --background   only start it (when Windows starts, see autostart.py)

Karya's screen is a page that Karya itself serves from this PC (http://127.0.0.1:<port>, "this computer"); it never
comes from the internet. Its window is Chrome (or Edge) in app mode with a profile of its own (data/app_window): no
tabs and no address bar, its own taskbar button, and the user's own browser windows, tabs and settings stay as they
are. Links in it open in the user's normal browser. Closing the window doesn't stop Karya, so WhatsApp tasks and bots
keep working; Setup > Safety & more > Quit Karya stops it."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import DATA_DIR, LOG_DIR, ROOT, settings

APP_PROFILE = DATA_DIR / "app_window"
LOG_FILE = LOG_DIR / "karya.log"
TITLE = "Karya"
START_WAIT = 90.0
NO_WINDOW = 0x08000000           # CREATE_NO_WINDOW: a console program without a console window
DETACHED = 0x00000008            # DETACHED_PROCESS
NEW_GROUP = 0x00000200           # CREATE_NEW_PROCESS_GROUP
BREAKAWAY = 0x01000000           # CREATE_BREAKAWAY_FROM_JOB: keeps running after whatever started it closes
WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------- the window
def browser_candidates() -> list[Path]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    chrome = Path("Google") / "Chrome" / "Application" / "chrome.exe"
    edge = Path("Microsoft") / "Edge" / "Application" / "msedge.exe"
    out = [Path(pf) / chrome, Path(pf86) / chrome]
    if local:
        out.append(Path(local) / chrome)
    return out + [Path(pf86) / edge, Path(pf) / edge]


def find_browser() -> str | None:
    """Chrome if it's installed, else Edge (every Windows 10 and 11 PC has it)."""
    for path in browser_candidates():
        if path.is_file():
            return str(path)
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def window_command(browser: str, url: str, profile: Path = APP_PROFILE) -> list[str]:
    args = [browser, f"--user-data-dir={profile}", f"--app={url}", "--no-first-run", "--no-default-browser-check",
            "--hide-crash-restore-bubble", "--disable-features=Translate"]
    if not (Path(profile) / "Default").exists():
        args.append("--window-size=1240,880")       # the first time; after that it opens where the user left it
    return args


def app_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?token={token}&app=1"


def open_window(url: str) -> bool:
    """Karya's own window. False when no Chrome or Edge was found (then it opens in the default browser)."""
    browser = find_browser()
    if browser:
        try:
            subprocess.Popen(window_command(browser, url), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=DETACHED if WINDOWS else 0)
            return True
        except OSError:
            pass
    webbrowser.open(url)
    return False


def open_in_browser(url: str) -> None:
    """A link from Karya's window opens in the user's normal browser (with their logins), not in Karya's window."""
    if WINDOWS:
        os.startfile(url)  # noqa: S606 - only http(s) links reach this (the server checks)
    else:
        webbrowser.open(url)


def _title(user32, hwnd) -> str:
    import ctypes
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _exe(hwnd) -> str:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def find_window():
    """Karya's open window: a Chrome or Edge app window titled exactly "Karya" (a Karya tab in a normal browser
    window is titled "Karya - Google Chrome"). None if there's none."""
    if not WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _title(user32, hwnd) == TITLE and _exe(hwnd) in ("chrome.exe", "msedge.exe"):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def focus(hwnd) -> bool:
    if not WINDOWS or not hwnd:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)                                   # SW_RESTORE
    return bool(user32.SetForegroundWindow(hwnd))


# ---------------------------------------------------------------- Karya in the background
def is_karya(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            return b"<title>Karya</title>" in resp.read(4000)
    except OSError:
        return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def read_token() -> str:
    try:
        return (DATA_DIR / ".token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _rotate(path: Path, limit: int = 2_000_000) -> None:
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_suffix(".old.log"))
    except OSError:
        pass


def server_python(root: Path = ROOT) -> Path:
    """pythonw.exe: Python without a console. (A venv's python.exe is a small launcher that starts the real Python
    in a second process, and its console window showed up even when asked not to.)"""
    scripts = Path(root) / ".venv" / "Scripts"
    for candidate in (scripts / "pythonw.exe", scripts / "python.exe", Path(sys.executable).with_name("pythonw.exe"),
                      Path(sys.executable)):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def start_server(root: Path = ROOT) -> subprocess.Popen:
    """Karya's server, without a console window; what it prints goes to data/logs/karya.log."""
    root = Path(root)
    python = server_python(root)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _rotate(LOG_FILE)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    args = [str(python), "-X", "utf8", "-m", "karya.server", "--no-browser"]
    with open(LOG_FILE, "ab") as log:
        log.write(f"\n---- {time.strftime('%Y-%m-%d %H:%M:%S')} Karya starting\n".encode())
        log.flush()
        flags = (NO_WINDOW | NEW_GROUP) if WINDOWS else 0
        for extra in ((BREAKAWAY, 0) if WINDOWS else (0,)):
            try:
                return subprocess.Popen(args, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, close_fds=True, creationflags=flags | extra)
            except OSError:
                if extra == 0:
                    raise
    raise RuntimeError("unreachable")


def wait_ready(port: int, proc=None, timeout: float = START_WAIT) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if is_karya(port):
            return True
        if proc is not None and proc.poll() is not None:     # it stopped: no point waiting
            return is_karya(port)
        time.sleep(0.5)
    return False


def ensure_env(root: Path = ROOT) -> None:
    env, example = Path(root) / ".env", Path(root) / ".env.example"
    if not env.exists() and example.exists():
        shutil.copyfile(example, env)
        settings.reload()


def ensure_ollama() -> None:
    """The free offline AI's server, if Ollama is installed and not running (as start.bat does)."""
    cli = shutil.which("ollama")
    if not cli:
        return
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2).close()
        return
    except OSError:
        pass
    try:
        subprocess.Popen([cli, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=(NO_WINDOW | NEW_GROUP) if WINDOWS else 0)
    except OSError:
        pass


def message(text: str, error: bool = True) -> None:
    if WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "Karya", 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr)


def _log_tail(lines: int = 8) -> str:
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(line for line in text[-lines:] if line.strip())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    background = "--background" in argv
    ensure_env()
    ensure_ollama()
    port = settings.port
    if not is_karya(port):
        if not port_free(port):
            # Karya may be starting already (opened twice), or another program uses the port
            if not wait_ready(port, None, 30):
                if not background:
                    message(f"Karya couldn't start: port {port} is used by another program. Set a different PORT in "
                            f"the .env file in {ROOT} and open Karya again.")
                return 1
        else:
            proc = start_server()
            if not wait_ready(port, proc):
                if not background:
                    tail = _log_tail()
                    message("Karya couldn't start." + (f"\n\nWhat it said:\n{tail}" if tail else "") +
                            f"\n\nThe full log is in {LOG_FILE}")
                return 1
    if background:
        return 0
    hwnd = find_window()
    if hwnd:
        focus(hwnd)
        return 0
    open_window(app_url(port, read_token()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
