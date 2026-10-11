"""Karya as an app: its own window (karya/desktop.py), the Start menu and desktop shortcuts (karya/shortcuts.py),
and the server's quit / open-link / shortcut endpoints."""
import sys
import time

import pytest
from fastapi.testclient import TestClient

from karya import config, desktop, kiro_bridge, shortcuts
from karya.agent import Agent
from karya.config import ROOT

from .conftest import FakeLLM

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for key in config.EDITABLE_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(kiro_bridge, "logout", lambda: None)
    config.settings.reload()
    yield path
    monkeypatch.undo()
    config.settings.reload()


# ---------------------------------------------------------------- the window
def test_chrome_first_then_edge(tmp_path, monkeypatch):
    chrome, edge = tmp_path / "chrome.exe", tmp_path / "msedge.exe"
    monkeypatch.setattr(desktop, "browser_candidates", lambda: [chrome, edge])
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.find_browser() is None
    edge.write_text("x")
    assert desktop.find_browser() == str(edge)
    chrome.write_text("x")
    assert desktop.find_browser() == str(chrome)


def test_the_window_has_its_own_profile_and_no_tabs(tmp_path):
    url = desktop.app_url(8765, "tok")
    assert url == "http://127.0.0.1:8765/?token=tok&app=1"
    cmd = desktop.window_command("chrome.exe", url, tmp_path / "profile")
    assert cmd[0] == "chrome.exe" and f"--user-data-dir={tmp_path / 'profile'}" in cmd and f"--app={url}" in cmd
    assert "--no-first-run" in cmd and "--window-size=1240,880" in cmd                # the first time only
    (tmp_path / "profile" / "Default").mkdir(parents=True)
    assert not any(a.startswith("--window-size") for a in desktop.window_command("chrome.exe", url, tmp_path / "profile"))


def test_without_chrome_or_edge_it_opens_in_the_default_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "find_browser", lambda: None)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))
    assert desktop.open_window("http://127.0.0.1:8765/") is False and opened == ["http://127.0.0.1:8765/"]


# ---------------------------------------------------------------- the launcher
class Proc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


@pytest.fixture()
def launcher(monkeypatch, tmp_path):
    state = {"up": False, "started": [], "opened": [], "focused": [], "window": None, "messages": []}

    def start(root=ROOT):
        state["started"].append(root)
        state["up"] = True
        return Proc()
    monkeypatch.setattr(desktop, "is_karya", lambda port: state["up"])
    monkeypatch.setattr(desktop, "port_free", lambda port: True)
    monkeypatch.setattr(desktop, "start_server", start)
    monkeypatch.setattr(desktop, "ensure_ollama", lambda: None)
    monkeypatch.setattr(desktop, "ensure_env", lambda root=ROOT: None)
    monkeypatch.setattr(desktop, "find_window", lambda: state["window"])
    monkeypatch.setattr(desktop, "focus", lambda hwnd: state["focused"].append(hwnd) or True)
    monkeypatch.setattr(desktop, "open_window", lambda url: state["opened"].append(url) or True)
    monkeypatch.setattr(desktop, "read_token", lambda: "tok123")
    monkeypatch.setattr(desktop, "message", lambda text, error=True: state["messages"].append(text))
    monkeypatch.setattr(desktop, "LOG_FILE", tmp_path / "karya.log")
    return state


def test_launcher_starts_karya_hidden_then_opens_its_window(launcher):
    assert desktop.main([]) == 0
    assert launcher["started"] and launcher["opened"] == [desktop.app_url(config.settings.port, "tok123")]
    # Karya is running and its window is open: it comes to the front, no second Karya and no second window
    launcher["started"].clear(), launcher["opened"].clear()
    launcher["window"] = 4242
    assert desktop.main([]) == 0
    assert launcher["focused"] == [4242] and not launcher["started"] and not launcher["opened"]


def test_launcher_at_windows_sign_in_only_starts_it(launcher):
    assert desktop.main(["--background"]) == 0
    assert launcher["started"] and not launcher["opened"] and not launcher["focused"]


def test_launcher_says_why_karya_didnt_start(launcher, monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "start_server", lambda root=ROOT: Proc(code=1))
    (tmp_path / "karya.log").write_text("starting\nModuleNotFoundError: No module named 'fastapi'\n", encoding="utf-8")
    assert desktop.main([]) == 1
    assert "couldn't start" in launcher["messages"][0] and "No module named 'fastapi'" in launcher["messages"][0]
    assert not launcher["opened"]
    launcher["messages"].clear()
    assert desktop.main(["--background"]) == 1 and launcher["messages"] == []      # at sign-in: no pop-up


def test_launcher_when_another_program_has_the_port(launcher, monkeypatch):
    monkeypatch.setattr(desktop, "port_free", lambda port: False)
    monkeypatch.setattr(desktop, "wait_ready", lambda port, proc=None, timeout=0: False)
    assert desktop.main([]) == 1 and "is used by another program" in launcher["messages"][0]
    assert not launcher["started"]


def test_the_server_runs_without_a_console_and_logs_to_a_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or Proc())
    monkeypatch.setattr(desktop, "LOG_FILE", tmp_path / "logs" / "karya.log")
    desktop.start_server(ROOT)
    args, kw = calls[0]
    assert args[1:] == ["-X", "utf8", "-m", "karya.server", "--no-browser"] and kw["cwd"] == str(ROOT)
    if (ROOT / ".venv" / "Scripts" / "pythonw.exe").exists():
        assert args[0].lower().endswith("pythonw.exe")                          # never a console window
    assert kw["env"]["PYTHONUTF8"] == "1" and "Karya starting" in (tmp_path / "logs" / "karya.log").read_text()
    if sys.platform == "win32":
        assert kw["creationflags"] & desktop.NO_WINDOW


def test_wait_ready_stops_when_karya_exits(monkeypatch):
    monkeypatch.setattr(desktop, "is_karya", lambda port: False)
    started = time.time()
    assert desktop.wait_ready(8765, Proc(code=1), timeout=30) is False and time.time() - started < 2


# ---------------------------------------------------------------- shortcuts
@windows_only
def test_start_menu_and_desktop_shortcuts(env, tmp_path):
    assert shortcuts.app_status() == {"supported": True, "start_menu": False, "desktop": False}
    shortcuts.set_place("start_menu", True)
    path = shortcuts.path_for("start_menu")
    assert path.parent == tmp_path / "StartMenu"                                   # never the real Start menu
    made = shortcuts.read(path)
    target, args = shortcuts.launcher(ROOT)
    assert made["target"].lower() == target.lower() and made["args"] == args and made["dir"].lower() == str(ROOT).lower()
    assert made["icon"].lower().startswith(str(ROOT / "karya" / "static" / "karya.ico").lower())
    assert "KARYA_START_MENU=true" in env.read_text(encoding="utf-8")
    shortcuts.set_place("desktop", True)
    assert shortcuts.app_status() == {"supported": True, "start_menu": True, "desktop": True}
    shortcuts.set_place("start_menu", False)
    assert not path.exists() and "KARYA_START_MENU=false" in env.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        shortcuts.add("desktop", tmp_path / "no-karya-here")                       # never a shortcut to nothing


@windows_only
def test_the_first_start_adds_karya_to_the_start_menu_once(env):
    assert shortcuts.first_run(ROOT) is True and shortcuts.app_status()["start_menu"]
    assert shortcuts.first_run(ROOT) is False                                      # once
    shortcuts.set_place("start_menu", False)
    assert shortcuts.first_run(ROOT) is False and not shortcuts.path_for("start_menu").exists()   # stays off


@windows_only
def test_a_shortcut_to_another_copy_of_karya_is_left_alone(env, tmp_path):
    other = tmp_path / "OtherKarya"
    other.mkdir()
    (other / "start.bat").write_text("@echo off", encoding="utf-8")
    shortcuts.add("desktop", other)
    assert shortcuts.app_status()["desktop"] is False                             # it isn't this Karya's
    shortcuts.remove("desktop", ROOT)
    assert shortcuts.path_for("desktop").exists()


# ---------------------------------------------------------------- the server
def test_quit_open_link_and_shortcut_endpoints(env, monkeypatch):
    from karya import server as server_mod
    app = server_mod.create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="tk", port=8765,
                                extra_hosts={"testserver"})
    opened = []
    monkeypatch.setattr(desktop, "open_in_browser", lambda url: opened.append(url))
    with TestClient(app) as client:
        assert client.post("/api/quit").status_code == 401
        assert client.post("/api/quit?token=tk").status_code == 400               # not started as the app
        stopped = []
        app.state.quit = lambda: stopped.append(True)
        assert client.post("/api/quit?token=tk").json() == {"quitting": True, "start_menu": False}
        time.sleep(0.8)
        assert stopped == [True]
        assert client.post("/api/open?token=tk", json={"url": "https://example.com/jobs/1"}).json() == {"opened": True}
        for bad in ("javascript:alert(1)", "file:///C:/Windows/system32", "ms-settings:privacy", ""):
            assert client.post("/api/open?token=tk", json={"url": bad}).status_code == 400
        assert client.post("/api/open?token=tk", json={"url": "https://example.com"},
                           headers={"origin": "https://evil.example"}).status_code == 401
        assert opened == ["https://example.com/jobs/1"]
        st = client.get("/api/status?token=tk").json()
        assert st["app"]["start_menu"] is False and "autostart" in st
        if sys.platform == "win32":
            assert client.post("/api/app?token=tk", json={"desktop": True}).json()["desktop"] is True
            assert client.post("/api/app?token=tk", json={"desktop": False}).json()["desktop"] is False
