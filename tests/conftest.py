import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import karya.tools  # noqa: E402,F401 - registers tools
from karya import agent as agent_mod  # noqa: E402
from karya import memory  # noqa: E402
from karya.config import settings  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs internet")
    config.addinivalue_line("markers", "browser: launches Chrome")
    config.addinivalue_line("markers", "ollama: needs the local Ollama model (slow)")


def pytest_collection_modifyitems(config, items):
    skip_live = os.environ.get("KARYA_SKIP_LIVE") == "1"
    run_ollama = os.environ.get("KARYA_RUN_OLLAMA") == "1"
    for item in items:
        if skip_live and "live" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="KARYA_SKIP_LIVE=1"))
        if "ollama" in item.keywords and not run_ollama:
            item.add_marker(pytest.mark.skip(reason="set KARYA_RUN_OLLAMA=1 to run the slow local-model test"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own workspace, memory, tracker and log folder."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(settings, "workspace", ws)
    monkeypatch.setattr(memory.memory_store, "path", tmp_path / "memory.json")
    monkeypatch.setattr(memory.applications_store, "path", tmp_path / "applications.json")
    monkeypatch.setattr(agent_mod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(agent_mod, "HISTORY_FILE", tmp_path / "conversation.json")
    from karya import llm, vault
    from karya.tools import jobs, resume
    monkeypatch.setattr(vault, "VAULT_FILE", tmp_path / "vault.json")
    monkeypatch.setattr(llm, "LIMITS_FILE", tmp_path / "limits.json")
    monkeypatch.setattr(llm, "MODELS_FILE", tmp_path / "models.json")
    monkeypatch.setattr(resume, "MASTER_FILE", tmp_path / "resume_master.json")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    from karya.tools import funding, job_sources
    job_sources.clear_cache()
    monkeypatch.setattr(funding, "_LAST", {"time": 0.0, "by_name": {}})
    from karya import apply_queue
    monkeypatch.setattr(apply_queue, "QUEUE_FILE", tmp_path / "cache" / "apply_queue.json")
    from karya import focus, outbox
    monkeypatch.setattr(outbox, "OUTBOX_FILE", tmp_path / "outbox.json")
    monkeypatch.setattr(outbox, "SEEN_FILE", tmp_path / "cache" / "seen_emails.json")
    monkeypatch.setattr(outbox, "_seen_cache", None)
    monkeypatch.setattr(outbox, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(focus, "CURRENT", {})
    monkeypatch.setattr(settings, "keep_going", False)
    # Never touch the user's real browser or accounts from a test: an empty throw-away profile, no visible window,
    # Karya's own window (not the user's Chrome), and no sending through Gmail in a browser unless a test fakes it.
    from karya.tools import browser, email_tools
    monkeypatch.setattr(browser.session, "profile_dir", tmp_path / "browser_profile")
    monkeypatch.setattr(settings, "browser_headless", True)
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(email_tools, "_send_with_gmail_web",
                        lambda *a, **k: email_tools.NOT_READY + " (tests never send through a browser)")
    yield tmp_path


def tool_call(name, args, call_id="call_1"):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def reply(text):
    return {"role": "assistant", "content": text}


class FakeProvider:
    name = "fake"
    model = "scripted"
    label = "fake:scripted"
    context_chars = 100_000


class FakeLLM:
    """Returns scripted assistant messages in order and records what it was sent."""

    def __init__(self, script, provider_name="fake"):
        self.script = list(script)
        self.calls = []
        self.provider = FakeProvider()
        self.provider.name = provider_name
        self.provider.compact_tools = provider_name in ("ollama", "groq")

    def chat(self, messages, tools=None, trim=None, notify=None, cancel=None, system_for=None):
        if callable(tools):
            tools = tools(self.provider)
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.script:
            return reply("(script finished)"), self.provider
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, self.provider


class Recorder:
    def __init__(self, answers=None):
        self.events = []
        self.requests = []
        self.answers = list(answers or [])

    async def emit(self, event):
        self.events.append(event)

    async def confirm(self, request):
        self.requests.append(request)
        return self.answers.pop(0) if self.answers else False

    def types(self):
        return [e["type"] for e in self.events]
