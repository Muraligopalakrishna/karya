"""Start Karya when the user signs in to Windows (karya/autostart.py), and staying reachable on WhatsApp."""
import sys

import pytest
from fastapi.testclient import TestClient

from karya import autostart, awake, phone, whatsapp
from karya.agent import Agent
from karya.config import ROOT, settings

from .conftest import FakeLLM

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows startup shortcuts")


@windows_only
def test_the_startup_shortcut_runs_karya_minimized_and_can_be_removed(tmp_path):
    assert autostart.status() == {"supported": True, "on": False}
    assert autostart.enable(ROOT) == {"supported": True, "on": True}
    assert autostart.shortcut_path().parent == tmp_path / "Startup"            # the test's folder, not the real one
    made = autostart.read_shortcut()
    assert made["target"].lower() == str(ROOT / "start.bat").lower() and made["args"] == "--no-browser"
    assert made["dir"].lower() == str(ROOT).lower() and made["style"] == 7     # 7 = minimized
    assert autostart.disable() == {"supported": True, "on": False} and not autostart.shortcut_path().exists()
    with pytest.raises(ValueError):
        autostart.enable(tmp_path / "no-karya-here")                          # never a shortcut to nothing


@windows_only
def test_autostart_endpoint_and_status():
    from karya import server as server_mod
    client = TestClient(server_mod.create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="tk", port=8765,
                                              extra_hosts={"testserver"}))
    assert client.post("/api/autostart", json={"on": True}).status_code == 401
    assert client.post("/api/autostart?token=tk", json={"on": True}).json() == {"supported": True, "on": True}
    assert client.get("/api/status?token=tk").json()["autostart"] == {"supported": True, "on": True}
    assert client.post("/api/autostart?token=tk", json={"on": True},
                       headers={"origin": "https://evil.example"}).status_code == 401
    assert client.post("/api/autostart?token=tk", json={"on": False}).json()["on"] is False


def test_whatsapp_waits_for_the_internet_after_windows_starts(monkeypatch):
    monkeypatch.setattr(whatsapp, "RETRY_SECONDS", 0)
    bridge = whatsapp.WhatsAppBridge("917000000000", lambda text: None)

    class Page:
        tries, closed = 0, False

        def goto(self, url, **kw):
            self.tries += 1
            if self.tries < 3:
                raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")

        def is_closed(self):
            return self.closed

    page = Page()
    assert bridge._open(page) is True and page.tries == 3
    assert bridge.detail == "waiting for the internet to open WhatsApp Web"
    shut = Page()
    shut.closed = True
    assert bridge._open(shut) is False                                         # its window was closed


def test_keep_awake_also_while_whatsapp_is_linked(monkeypatch):
    class Channel:
        def __init__(self, state):
            self.state = state

        def status(self):
            return {"state": self.state}

    class Hub:
        bots = {}

    monkeypatch.setattr(settings, "keep_awake", True)
    monkeypatch.setattr(phone, "CHANNEL", Channel("ready"))
    assert awake.wanted(Hub()) is True
    monkeypatch.setattr(phone, "CHANNEL", Channel("off"))
    assert awake.wanted(Hub()) is False                                        # no bots, no schedules, no WhatsApp
    monkeypatch.setattr(settings, "keep_awake", False)
    monkeypatch.setattr(phone, "CHANNEL", Channel("ready"))
    assert awake.wanted(Hub()) is False                                        # only when the user turned it on
