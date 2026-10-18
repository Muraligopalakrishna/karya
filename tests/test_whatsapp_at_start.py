"""WhatsApp opens by itself only when the user wants that (2026-10-18: "whatsapp open every time karya opens, i
don't want that"). Otherwise it opens when they ask: Setup, the chat, or an AI app through MCP."""
import contextlib
import threading

import playwright.sync_api
import pytest

from karya import mcp_server, phone, whatsapp
from karya.config import settings


@pytest.fixture()
def owner(monkeypatch):
    monkeypatch.setattr(phone, "owner_number", lambda: "919999999999")
    monkeypatch.setattr(phone, "CHANNEL", None)


def _started(monkeypatch):
    opened = threading.Event()
    monkeypatch.setattr(phone.PhoneChannel, "enable", lambda self, wait=25.0: opened.set() or {"state": "starting"})
    return opened


def test_linked_whatsapp_doesnt_open_at_start_unless_turned_on(monkeypatch, owner):
    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_at_start", False)
    opened = _started(monkeypatch)
    phone.start(object(), None)
    assert not opened.wait(0.5)

    monkeypatch.setattr(settings, "whatsapp_at_start", True)
    opened = _started(monkeypatch)
    phone.start(object(), None)
    assert opened.wait(3)


def test_at_start_is_a_setting_the_user_can_change():
    from karya.config import EDITABLE_KEYS
    assert "WHATSAPP_AT_START" in EDITABLE_KEYS


class _Ctx:
    pages = [object()]

    def close(self):
        pass


def _bridge(monkeypatch, endings):
    """A bridge whose window 'ends' the given ways in turn ("closed" by the user, "stuck"); then Karya stops."""
    b = whatsapp.WhatsAppBridge("919999999999", lambda text: None)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(whatsapp, "REOPEN_SECONDS", 0)
    monkeypatch.setattr(b, "_launch", lambda pw: _Ctx())
    monkeypatch.setattr(b, "_open", lambda page: True)
    windows = []

    def loop(page):
        windows.append(1)
        if len(windows) > len(endings):
            b.stop()
            return None
        return endings[len(windows) - 1]

    monkeypatch.setattr(b, "_loop", loop)
    return b, windows


def test_a_window_the_user_closed_stays_closed(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_at_start", False)
    b, windows = _bridge(monkeypatch, ["closed"])
    b._run()
    assert len(windows) == 1                       # not opened again
    assert b.state == "off" and b.detail == whatsapp.CLOSED_BY_USER
    assert "open it again" in b.detail


def test_a_stuck_window_is_still_reopened(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_at_start", False)
    b, windows = _bridge(monkeypatch, ["stuck"])
    b._run()
    assert len(windows) == 2


def test_always_open_reopens_a_closed_window(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_at_start", True)
    b, windows = _bridge(monkeypatch, ["closed"])
    b._run()
    assert len(windows) == 2


class _Hub:
    source = "phone"


def test_approvals_stay_in_karya_while_whatsapp_is_closed(monkeypatch):
    ch = phone.PhoneChannel(_Hub(), None)
    bot_request = {"id": "r1", "bot": "Maya", "summary": "Submit the application"}
    assert ch._routed(bot_request) is False        # no WhatsApp window at all

    class Closed:
        state, out = "off", []

        def send(self, text):
            self.out.append(text)

    ch.bridge = Closed()
    assert ch._routed(bot_request) is False
    ch.bridge.state = "ready"
    assert ch._routed(bot_request) is True


def test_status_says_how_to_open_whatsapp(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_at_start", False)
    st = phone.PhoneChannel(_Hub(), None).status()
    assert st["state"] == "off" and st["opened_before"] is True and st["at_start"] is False
    assert "Open WhatsApp" in st["detail"]


def test_ai_apps_can_open_whatsapp_through_mcp():
    assert "connect_whatsapp" in mcp_server.DEFAULT_TOOLS
    assert len(mcp_server.DEFAULT_TOOLS) <= 40      # Cursor takes about 40 tools in total
