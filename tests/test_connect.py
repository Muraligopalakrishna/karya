"""Setup > Connect your AI (karya/connect.py, codex_bridge.py, Kiro sign-in) and one-click MCP (karya/mcp_apps.py)."""
import json
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from karya import codex_bridge, config, connect, kiro_bridge, mcp_apps
from karya.agent import Agent

from .conftest import FakeLLM


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A throw-away .env; settings rebuilt from it; the real settings come back afterwards."""
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", path)
    for key in config.EDITABLE_KEYS + ("OLLAMA_ENABLED",):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    monkeypatch.setattr(kiro_bridge, "logout", lambda: None)
    config.settings.reload()
    yield path
    monkeypatch.undo()
    config.settings.reload()


def names():
    return [p.name for p in config.settings.providers]


# ---------------------------------------------------------------- keys
@pytest.mark.parametrize("key,want", [
    ("ksk_" + "a" * 40, ["kiro"]), ("sk-ant-api03-" + "b" * 40, ["anthropic"]), ("sk-or-v1-" + "c" * 40, ["openrouter"]),
    ("gsk_" + "d" * 40, ["groq"]), ("AIza" + "E" * 35, ["gemini"]), ("xai-" + "f" * 40, ["xai"]),
    ("csk-" + "g" * 40, ["cerebras"]), ("sk-proj-" + "h" * 40, ["openai"]), ("sk-" + "0a" * 16, ["deepseek", "openai"]),
    ("sk-" + "Ab1" * 16, ["openai", "deepseek"]), ("A1b2" * 8, ["mistral"]), ("what is this", [])])
def test_detect_key(key, want):
    assert config.detect_key(key) == want


def test_paste_a_key_and_it_goes_to_the_right_service(env):
    out = connect.save_key("  gsk_" + "x" * 40 + "  ")
    assert out == {"saved": "groq", "title": "Groq"}
    assert "GROQ_API_KEY=gsk_" in env.read_text(encoding="utf-8") and names() == ["groq"]
    assert "LLM_PROVIDERS" not in env.read_text(encoding="utf-8")       # the default order is kept
    assert connect.save_key("sk-" + "0a" * 16) == {"choose": ["deepseek", "openai"]}   # never sent anywhere to guess
    assert connect.save_key("sk-" + "0a" * 16, service="deepseek")["saved"] == "deepseek"
    assert "choose" in connect.save_key("Zz9" * 9 + "-q")                # unknown shape: the user picks
    with pytest.raises(ValueError):
        connect.save_key("two words")
    with pytest.raises(ValueError):
        connect.save_key("gsk_" + "x" * 40, service="nope")


def test_order_first_and_new_ones_are_never_dropped(env):
    connect.save_key("gsk_" + "x" * 40)
    connect.save_key("AIza" + "E" * 35)
    assert names() == ["gemini", "groq"]
    connect.use_first("groq")
    assert names() == ["groq", "gemini"]
    connect.save_key("sk-or-v1-" + "c" * 40)                             # a custom order gets the new one too
    assert names() == ["groq", "gemini", "openrouter"]
    connect.remove("gemini")
    assert names() == ["groq", "openrouter"] and "GEMINI_API_KEY=\n" in env.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        connect.use_first("gemini")
    with pytest.raises(ValueError):
        connect.remove("ollama")


def test_pick_a_model(env, monkeypatch):
    connect.save_key("gsk_" + "x" * 40)
    connect.set_model("groq", "openai/gpt-oss-20b")
    assert next(p for p in config.settings.providers if p.name == "groq").model == "openai/gpt-oss-20b"
    with pytest.raises(ValueError):
        connect.set_model("groq", "x; del C:\\")
    with pytest.raises(ValueError):
        connect.set_model("openai", "gpt-5.5")                          # not connected

    class Models:
        data = [type("M", (), {"id": i}) for i in ("models/gemini-3.8-flash", "text-embedding-3", "whisper-1",
                                                   "openai/gpt-oss-120b")]

    class Client:
        models = type("L", (), {"list": staticmethod(lambda: Models)})

    from karya import llm
    monkeypatch.setattr(llm.LLMClient, "_client", lambda self, p: Client)
    ids = [m["id"] for m in connect.models_for("groq", fresh=True)]
    assert ids == ["gemini-3.8-flash", "openai/gpt-oss-120b"]            # only chat models, no "models/" prefix


# ---------------------------------------------------------------- subscriptions
def test_kiro_sign_in_without_a_key(env, monkeypatch):
    monkeypatch.setattr(kiro_bridge, "login_progress", lambda: {"state": "done"})
    connect.save_key("gsk_" + "x" * 40)
    assert connect.login_progress("kiro") == {"state": "done"}
    kiro = config.settings.providers[0]
    assert kiro.name == "kiro" and kiro.api_key == "login" and names() == ["kiro", "groq"]
    assert "KIRO_API_KEY" not in kiro_bridge.private_env("login")
    assert kiro_bridge.private_env("ksk_" + "z" * 20)["KIRO_API_KEY"].startswith("ksk_")
    assert kiro_bridge.private_env("login")["USERPROFILE"] == str(kiro_bridge.HOME)
    connect.remove("kiro")
    assert names() == ["groq"]


def test_chatgpt_plan_through_codex(env, monkeypatch):
    monkeypatch.setattr(codex_bridge, "login_progress", lambda: {"state": "done"})
    assert connect.login_progress("codex") == {"state": "done"}
    codex = config.settings.providers[0]
    assert (codex.name, codex.model, codex.api_key) == ("codex", "gpt-5.4-mini", "login")
    connect.set_model("codex", "gpt-5.5")
    assert config.settings.providers[0].model == "gpt-5.5"
    monkeypatch.setattr(codex_bridge, "login_status", lambda fresh=False: {"installed": True, "signed_in": False})
    connect.remove("codex")
    with pytest.raises(ValueError):
        connect.use_codex()                                              # not signed in: nothing changes
    assert names() == []


FAKE_CODEX = textwrap.dedent('''
    import json, sys
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    out = lambda e: print(json.dumps(e), flush=True)
    out({"type": "thread.started", "thread_id": "t1"})
    if "AUTH" in prompt:
        out({"type": "error", "message": "Reconnecting... 2/5 (unexpected status 401 Unauthorized)"})
        sys.exit(1)
    flags = [args[i + 1] for i, a in enumerate(args) if a == "--disable"]
    safe = ("-s" in args and args[args.index("-s") + 1] == "read-only" and "--ephemeral" in args
            and "--ignore-user-config" in args and 'approval_policy="never"' in args)
    model = args[args.index("-m") + 1] if "-m" in args else ""
    out({"type": "item.completed", "item": {"type": "agent_message",
         "text": f"model={model} safe={safe} off={','.join(flags)} chars={len(prompt)}"}})
    out({"type": "turn.completed", "usage": {"input_tokens": 1234, "output_tokens": 5}})
''')


def test_codex_brain_runs_text_only_and_reads_the_answer(tmp_path, monkeypatch):
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    monkeypatch.setattr(codex_bridge, "find_cli", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(codex_bridge, "_features_off", lambda: ["--disable", "shell_tool", "--disable", "apps"])
    brain = codex_bridge.CodexBrain()
    answer = brain.prompt("x" * 5000, "gpt-5.5", timeout=60)
    assert answer == "model=gpt-5.5 safe=True off=shell_tool,apps chars=5000"
    assert brain.last_usage["input_tokens"] == 1234
    with pytest.raises(codex_bridge.CodexError) as err:
        brain.prompt("AUTH please", "gpt-5.5", timeout=60)
    assert err.value.kind == "auth"


def test_codex_steps_go_through_the_agent_format(monkeypatch):
    from karya import llm
    calls = []

    def fake_prompt(text, model, timeout=240, effort="low"):
        calls.append((model, effort, "web_search" in text))
        return '<tool_call>{"name": "web_search", "arguments": {"query": "gold price"}}</tool_call>'
    monkeypatch.setattr(codex_bridge.BRAIN, "prompt", fake_prompt)
    p = config.Provider("codex", "codex-cli://exec", "login", "gpt-5.4-mini", reasoning_effort="low", max_input_tokens=50_000)
    client = llm.LLMClient([p])
    tools = [{"type": "function", "function": {"name": "web_search", "description": "Search the web",
                                               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
    kind, msg = client._try(p, p.model, [{"role": "system", "content": "be Karya"}, {"role": "user", "content": "gold?"}],
                            tools, None, [])
    assert kind == "ok" and msg["tool_calls"][0]["function"]["name"] == "web_search"
    assert calls == [("gpt-5.4-mini", "low", True)]


def test_codex_feature_flags_only_names_this_version_has(monkeypatch):
    class R:
        stdout = (b"apps   stable   true\nshell_tool   stable   true\nweb_search_cached  deprecated  false\n"
                  b"remote_control   removed   false\nunified_exec  stable  false\n")
    monkeypatch.setattr(codex_bridge, "_run", lambda args, timeout=60, data=None: R)
    monkeypatch.setattr(codex_bridge, "_CACHE", {})
    assert codex_bridge._features_off() == ["--disable", "apps", "--disable", "shell_tool"]


# ---------------------------------------------------------------- the endpoint
def test_ai_endpoint(env, monkeypatch):
    from karya import server as server_mod
    monkeypatch.setattr(server_mod, "_check_providers",
                        lambda llm, names: [{"provider": n, "ok": True, "model": "m"} for n in sorted(names)])
    monkeypatch.setattr(connect, "overview", lambda include_logins=True: {"connected": names()})
    client = TestClient(server_mod.create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="tk", port=8765,
                                              extra_hosts={"testserver"}))
    assert client.post("/api/ai", json={"action": "key", "key": "gsk_" + "x" * 40}).status_code == 401
    r = client.post("/api/ai?token=tk", json={"action": "key", "key": "gsk_" + "x" * 40})
    assert r.status_code == 200 and r.json()["saved"] == "groq" and r.json()["checks"][0]["ok"]
    assert "gsk_" not in json.dumps(r.json())                           # the key never comes back
    assert client.post("/api/ai?token=tk", json={"action": "key", "key": "bad key"}).status_code == 400
    assert client.post("/api/ai?token=tk", json={"action": "first", "provider": "groq"}).json()["overview"] == \
        {"connected": ["groq"]}
    assert client.post("/api/ai?token=tk", json={"action": "key", "key": "gsk_" + "y" * 40},
                       headers={"origin": "https://evil.example"}).status_code == 401
    assert client.post("/api/ai?token=tk", json={"action": "explode"}).status_code == 400


# ---------------------------------------------------------------- one-click MCP
@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "home" / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "home" / "AppData" / "Local"))
    monkeypatch.setattr(mcp_apps.shutil, "which", lambda name: None)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_mcp_json_apps_are_merged_with_a_backup(home, tmp_path):
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}), encoding="utf-8")
    rows = {r["id"]: r for r in mcp_apps.overview(tmp_path / "Karya")}
    assert rows["cursor"]["installed"] and rows["cursor"]["state"] == "no" and not rows["windsurf"]["installed"]
    out = mcp_apps.connect("cursor", tmp_path / "Karya")
    assert out["state"] == "connected" and "Restart Cursor" in out["next"]
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["github"] == {"command": "gh-mcp"}
    assert data["mcpServers"]["karya"]["args"][0].endswith("karya_mcp.py")
    assert json.loads((cfg.parent / "mcp.json.before-karya").read_text(encoding="utf-8")) == \
        {"mcpServers": {"github": {"command": "gh-mcp"}}}
    assert mcp_apps.overview(tmp_path / "Other")[3]["state"] == "other"       # Karya from another folder
    gem = mcp_apps.connect("gemini", tmp_path / "Karya")                       # a new file is made
    assert gem["state"] == "connected"
    assert json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))["mcpServers"]["karya"]["timeout"] == 900000


def test_mcp_vscode_uses_servers_and_a_file_with_comments_is_left_alone(home, tmp_path):
    user = home / "AppData" / "Roaming" / "Code" / "User"
    user.mkdir(parents=True)
    mcp_apps.connect("vscode", tmp_path / "Karya")
    assert json.loads((user / "mcp.json").read_text(encoding="utf-8"))["servers"]["karya"]["type"] == "stdio"
    commented = '{\n  // my servers\n  "servers": {}\n}\n'
    (user / "mcp.json").write_text(commented, encoding="utf-8")
    assert next(r for r in mcp_apps.overview(tmp_path / "Karya") if r["id"] == "vscode")["state"] == "unreadable"
    with pytest.raises(ValueError):
        mcp_apps.connect("vscode", tmp_path / "Karya")
    assert (user / "mcp.json").read_text(encoding="utf-8") == commented


def test_mcp_codex_toml_block_is_replaced_not_duplicated(home, tmp_path):
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model = "gpt-5.5"\n\n[mcp_servers.karya]\ncommand = "C:\\\\Old\\\\python.exe"\nargs = []\n\n'
                   '[mcp_servers.other]\ncommand = "x"\n', encoding="utf-8")
    assert next(r for r in mcp_apps.overview(tmp_path / "Karya") if r["id"] == "codex")["state"] == "other"
    assert mcp_apps.connect("codex", tmp_path / "Karya")["state"] == "connected"
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.karya]") == 1 and "Old" not in text and "tool_timeout_sec = 900" in text
    assert 'model = "gpt-5.5"' in text and "[mcp_servers.other]" in text


def test_mcp_claude_desktop_store_build(home, tmp_path):
    store = home / "AppData" / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    row = next(r for r in mcp_apps.overview(tmp_path / "Karya") if r["id"] == "claude_desktop")
    assert row["installed"] and row["file"] == str(store / "claude_desktop_config.json")
    with pytest.raises(ValueError):
        mcp_apps.connect("claude_code", tmp_path / "Karya")                   # its CLI isn't installed here
    with pytest.raises(ValueError):
        mcp_apps.connect("nope", tmp_path / "Karya")
