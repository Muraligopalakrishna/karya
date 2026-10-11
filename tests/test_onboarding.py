"""Claude Code as Karya's brain (karya/claude_bridge.py), the free offline AI (karya/local_ai.py) and the first-run
guide (karya/onboarding.py)."""
import base64
import json
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from karya import claude_bridge, config, connect, kiro_bridge, local_ai, onboarding
from karya.agent import Agent

from .conftest import FakeLLM


@pytest.fixture()
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for key in config.EDITABLE_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    monkeypatch.setattr(kiro_bridge, "logout", lambda: None)
    config.settings.reload()
    yield path
    monkeypatch.undo()
    config.settings.reload()


def names():
    return [p.name for p in config.settings.providers]


# ---------------------------------------------------------------- Claude Code
FAKE_CLAUDE = textwrap.dedent('''
    import json, sys
    args = sys.argv[1:]
    if args[:1] == ["--help"]:
        print("--tools <tools...>  --safe-mode  --strict-mcp-config  --disable-slash-commands  "
              "--no-session-persistence  --permission-mode <mode>  --effort <level>  --system-prompt <prompt>")
        sys.exit(0)
    prompt = sys.stdin.read()
    out = lambda e: print(json.dumps(e), flush=True)
    out({"type": "system", "subtype": "init"})
    if "REFUSED" in prompt:
        out({"type": "system", "subtype": "api_retry", "error": "authentication_failed", "attempt": 1, "error_status": 403})
        import time; time.sleep(30)            # the real CLI would keep retrying for minutes
        sys.exit(1)
    if "BADMODEL" in prompt:
        out({"type": "result", "subtype": "success", "is_error": True,
             "result": "There's an issue with the selected model (x). It may not exist or you may not have access to it."})
        sys.exit(1)
    i = args.index("--tools")
    safe = (args[i + 1] == "" and "--safe-mode" in args and "--strict-mcp-config" in args and "--no-session-persistence" in args
            and args[args.index("--permission-mode") + 1] == "dontAsk" and "-p" in args)
    model = args[args.index("--model") + 1] if "--model" in args else ""
    text = f"model={model} safe={safe} chars={len(prompt)}"
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "usage": {"input_tokens": 99}})
''')


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    monkeypatch.setattr(claude_bridge, "find_cli", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(claude_bridge, "_CACHE", {})
    monkeypatch.setattr(claude_bridge, "WORKDIR", tmp_path / "claude_brain")
    return script


def test_claude_runs_text_only_and_reads_the_answer(fake_claude):
    brain = claude_bridge.ClaudeBrain()
    assert brain.prompt("x" * 3000, "sonnet", timeout=60) == "model=sonnet safe=True chars=3000"
    assert brain.last_usage == {"input_tokens": 99}


def test_claude_stops_at_once_when_its_login_is_refused(fake_claude):
    import time
    started = time.time()
    with pytest.raises(claude_bridge.ClaudeError) as err:
        claude_bridge.ClaudeBrain().prompt("REFUSED", "sonnet", timeout=60)
    assert err.value.kind == "auth" and time.time() - started < 15            # not after minutes of retries
    with pytest.raises(claude_bridge.ClaudeError) as err:
        claude_bridge.ClaudeBrain().prompt("BADMODEL", "opus", timeout=60)
    assert err.value.kind == "model"


def test_claude_without_a_way_to_switch_tools_off_is_not_used(monkeypatch):
    monkeypatch.setattr(claude_bridge, "_CACHE", {"help": "--disallowedTools <tools...>  --safe-mode"})
    assert claude_bridge.text_only_flags()[:2] == ["--disallowedTools", claude_bridge.text_only_flags()[1]]
    assert "Bash" in claude_bridge.text_only_flags()[1] and "--safe-mode" in claude_bridge.text_only_flags()
    monkeypatch.setattr(claude_bridge, "_CACHE", {"help": "-p --print  --model <model>"})
    with pytest.raises(claude_bridge.ClaudeError) as err:
        claude_bridge.text_only_flags()
    assert err.value.kind == "missing"


@pytest.mark.parametrize("data,method", [
    ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}, "Claude plan"),
    ({"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"}, "AWS Bedrock"),
    ({"loggedIn": True, "authMethod": "api_key"}, "API key"),
])
def test_claude_sign_in_states(monkeypatch, data, method):
    class R:
        stdout = json.dumps(data).encode()
    monkeypatch.setattr(claude_bridge, "find_cli", lambda: ["claude"])
    monkeypatch.setattr(claude_bridge, "_run", lambda args, timeout=60: R)
    monkeypatch.setattr(claude_bridge, "_CACHE", {})
    assert claude_bridge.login_status(fresh=True) == {"installed": True, "signed_in": True, "method": method}


def test_claude_never_gets_karyas_keys(monkeypatch):
    monkeypatch.setattr(config, "ORIGINAL_ENV", {"PATH": "x", "CLAUDE_CODE_USE_BEDROCK": "1"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-karyas-env")
    env = claude_bridge._env()
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_claude_in_connect_your_ai(env, monkeypatch):
    monkeypatch.setattr(claude_bridge, "login_progress", lambda: {"state": "done"})
    assert connect.login_progress("claude") == {"state": "done"}
    claude = config.settings.providers[0]
    assert (claude.name, claude.model, claude.api_key) == ("claude", "sonnet", "login")
    assert [m["id"] for m in connect.models_for("claude", fresh=True)] == ["sonnet", "opus", "haiku", "fable"]
    connect.set_model("claude", "opus")
    assert config.settings.providers[0].model == "opus" and "CLAUDE_MODEL=opus" in env.read_text(encoding="utf-8")
    connect.remove("claude")
    assert names() == []
    monkeypatch.setattr(claude_bridge, "login_status", lambda fresh=False: {"installed": True, "signed_in": False})
    with pytest.raises(ValueError):
        connect.use_claude()
    monkeypatch.setattr(claude_bridge, "login_status", lambda fresh=False: {"installed": True, "signed_in": True})
    assert connect.use_claude() == {"enabled": "claude"} and names() == ["claude"]


def test_claude_steps_go_through_the_agent_format(monkeypatch):
    from karya import llm
    monkeypatch.setattr(claude_bridge.BRAIN, "prompt", lambda text, model, timeout=240, effort="low":
                        '<tool_call>{"name": "web_search", "arguments": {"query": "gold"}}</tool_call>')
    p = config.Provider("claude", "claude-cli://print", "login", "sonnet", reasoning_effort="low", max_input_tokens=60_000)
    tools = [{"type": "function", "function": {"name": "web_search", "description": "Search",
                                               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
    kind, msg = llm.LLMClient([p])._try(p, "sonnet", [{"role": "user", "content": "gold?"}], tools, None, [])
    assert kind == "ok" and msg["tool_calls"][0]["function"]["name"] == "web_search"


# ---------------------------------------------------------------- the free offline AI
class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def test_offline_ai_is_ready_only_once_the_model_is_downloaded(monkeypatch):
    tags = {"models": []}

    def get(url, timeout=0):
        if url.endswith("/api/version"):
            return Resp({"version": "0.34"})
        return Resp(tags)
    monkeypatch.setattr(local_ai.requests, "get", get)
    monkeypatch.setattr(local_ai, "find_cli", lambda: "ollama.exe")
    st = local_ai.status()
    assert st["installed"] and st["running"] and not st["ready"]          # Ollama alone isn't enough
    tags["models"] = [{"name": "karya-qwen:latest", "size": 3.4e9}]
    assert local_ai.status()["ready"] and local_ai.models()[0] == {"id": "karya-qwen", "name": "karya-qwen",
                                                                   "description": "3.4 GB on this PC"}
    monkeypatch.setattr(local_ai.requests, "get", lambda url, timeout=0: (_ for _ in ()).throw(local_ai.requests.ConnectionError()))
    monkeypatch.setattr(local_ai, "find_cli", lambda: None)
    assert local_ai.status() == {**local_ai.status(), "installed": False, "running": False, "ready": False}


def test_offline_ai_download_in_one_click(env, monkeypatch):
    calls = []
    state = {"pulled": False}
    monkeypatch.setattr(local_ai, "find_cli", lambda: "ollama.exe")
    monkeypatch.setattr(local_ai, "status", lambda cached=False: {"installed": True, "running": True, "ready": state["pulled"]})
    monkeypatch.setattr(local_ai, "_pull", lambda model: calls.append(("pull", model)) or state.update(pulled=True))
    monkeypatch.setattr(local_ai.subprocess, "run", lambda args, **kw: calls.append(("run", args[1:3])))
    monkeypatch.setattr(local_ai, "installed_models", lambda: [{"name": "karya-qwen:latest", "gb": 3.4}])
    monkeypatch.setattr(local_ai, "PULL", {"state": "idle", "percent": 0, "detail": "", "error": ""})
    local_ai.download()
    import time
    for _ in range(50):
        if local_ai.PULL["state"] == "done":
            break
        time.sleep(0.05)
    assert local_ai.PULL["state"] == "done"
    assert calls == [("pull", "qwen3.5:4b"), ("run", ["create", "karya-qwen"])]
    text = env.read_text(encoding="utf-8")
    assert "OLLAMA_ENABLED=true" in text and "OLLAMA_MODEL=karya-qwen" in text


# ---------------------------------------------------------------- the first-run guide
def test_guide_saves_the_users_details_and_resume(env, monkeypatch, tmp_path):
    from karya import memory
    imported = []
    monkeypatch.setattr(onboarding, "_import_in_background", lambda path: imported.append(path))
    st = onboarding.save_profile({"name": "  Asha   Rao ", "email": "asha@example.com", "location": "Pune, India",
                                  "salary": "ignored", "phone": ""})
    assert st["about"] and st["profile"] == {"name": "Asha Rao", "email": "asha@example.com", "location": "Pune, India"}
    assert "salary" not in memory.memory_store.load()["profile"]
    with pytest.raises(ValueError):
        onboarding.save_profile({"email": "not an email"})
    pdf = base64.b64encode(b"%PDF-1.7 my resume").decode()
    with pytest.raises(ValueError):
        onboarding.save_resume("resume.exe", pdf)
    with pytest.raises(ValueError):
        onboarding.save_resume("resume.pdf", base64.b64encode(b"MZ not a pdf").decode())
    with pytest.raises(ValueError):
        onboarding.save_resume("resume.pdf", "%%%")
    onboarding.save_resume("../../Asha Rao (2026).pdf", pdf)
    saved = onboarding.RESUME_DIR / "Asha Rao _2026_.pdf"
    assert saved.read_bytes() == b"%PDF-1.7 my resume" and imported == [saved]   # never outside data/resume
    assert f"RESUME_PATH={saved}" in env.read_text(encoding="utf-8")
    assert not onboarding.state()["done"]
    onboarding.finish(skipped=True)
    assert onboarding.state()["done"] and "KARYA_ONBOARDED=skipped" in env.read_text(encoding="utf-8")
    onboarding.reset()
    assert not onboarding.state()["done"]


def test_guide_shows_only_to_people_who_arent_set_up(env, monkeypatch):
    from karya import memory, server
    monkeypatch.setattr(local_ai, "status", lambda cached=False: {"ready": False})
    assert server._readiness() == {"ai_ready": False, "you": {"name": False, "resume": False}, "onboarded": False}
    connect.save_key("gsk_" + "x" * 40)
    assert server._readiness()["ai_ready"] and not server._readiness()["onboarded"]
    data = memory.memory_store.load()
    data.setdefault("profile", {})["name"] = "Asha"
    memory.memory_store.save(data)
    assert server._readiness()["onboarded"]                                # set up before the guide existed
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    config.settings.reload()
    connect.remove("groq")
    assert not server._readiness()["ai_ready"]                             # the offline AI counts only when ready


def test_guide_endpoint(env, monkeypatch):
    from karya import server as server_mod
    monkeypatch.setattr(onboarding, "_import_in_background", lambda path: None)
    client = TestClient(server_mod.create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="tk", port=8765,
                                              extra_hosts={"testserver"}))
    assert client.get("/api/onboarding").status_code == 401
    assert client.post("/api/onboarding?token=tk", json={"action": "profile", "profile": {"name": "Asha"}}).json()["about"]
    bad = client.post("/api/onboarding?token=tk", json={"action": "resume", "filename": "x.pdf", "data": "bm9wZQ=="})
    assert bad.status_code == 400 and "isn't a PDF" in bad.json()["error"]
    ok = client.post("/api/onboarding?token=tk", json={"action": "resume", "filename": "cv.pdf",
                                                       "data": base64.b64encode(b"%PDF-1.4 x").decode()})
    assert ok.status_code == 200 and ok.json()["resume_file"] == "" or ok.status_code == 200
    assert client.post("/api/onboarding?token=tk", json={"action": "done"}).json()["done"]
    assert client.get("/api/status?token=tk").json()["onboarded"] is True
    assert client.post("/api/onboarding?token=tk", json={"action": "profile", "profile": {"name": "x"}},
                       headers={"origin": "https://evil.example"}).status_code == 401
    assert client.post("/api/onboarding?token=tk", json={"action": "nope"}).status_code == 400


def test_use_claude_adds_it_only_if_it_answers(env, monkeypatch):
    """The user's PC, 2026-10-11: Claude Code was signed in through AWS Bedrock, but Bedrock refused its models."""
    from karya import server as server_mod
    monkeypatch.setattr(claude_bridge, "login_status", lambda fresh=False: {"installed": True, "signed_in": True,
                                                                            "method": "AWS Bedrock"})
    answers = {"ok": False}
    monkeypatch.setattr(server_mod, "_check_providers", lambda llm, names: [
        {"provider": n, "ok": answers["ok"], "error": None if answers["ok"] else "HTTP 403, refused"} for n in sorted(names)])
    monkeypatch.setattr(connect, "overview", lambda include_logins=True: {"connected": names()})
    client = TestClient(server_mod.create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="tk", port=8765,
                                              extra_hosts={"testserver"}))
    r = client.post("/api/ai?token=tk", json={"action": "use_claude"})
    assert r.status_code == 400 and "couldn't answer for Karya: HTTP 403, refused" in r.json()["error"]
    text = env.read_text(encoding="utf-8")
    assert "claude" not in names() and "CLAUDE_ENABLED=\n" in text and "LLM_PROVIDERS=\n" in text   # as before
    answers["ok"] = True
    r = client.post("/api/ai?token=tk", json={"action": "use_claude"})
    assert r.status_code == 200 and names()[0] == "claude"


def test_voice_models_are_left_out_of_model_lists(env, monkeypatch):
    connect.save_key("gsk_" + "x" * 40)

    class Models:
        data = [type("M", (), {"id": i}) for i in ("openai/gpt-oss-120b", "canopylabs/orpheus-v1-english",
                                                   "playai-tts", "allam-2-7b")]

    class Client:
        models = type("L", (), {"list": staticmethod(lambda: Models)})

    from karya import llm
    monkeypatch.setattr(llm.LLMClient, "_client", lambda self, p: Client)
    assert [m["id"] for m in connect.models_for("groq", fresh=True)] == ["allam-2-7b", "openai/gpt-oss-120b"]
