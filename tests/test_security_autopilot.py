"""Full access (autopilot), the security guards, reusing one login, and the posting playbooks.
From the user's 2026-10-06 request: do everything without asking, but keep checking; lock it down against misuse."""
import asyncio
import json

import pytest

from karya import autopilot, secrets_filter, vault
from karya.agent import Agent
from karya.config import ROOT, settings
from karya.registry import CONFIRM, CRITICAL, SAFE, P, TOOLS, run_tool, tool
from karya.tools import accounts as accounts_tools
from karya.tools import pc

from .conftest import FakeLLM, Recorder, reply, tool_call

RAN = []


@tool("_fa_post", "test: a post-like action", {"x": P("string", "x")}, risk=CRITICAL)
def _fa_post(x: str = ""):
    RAN.append(("post", x))
    return f'Clicked "Post {x}".\nRESULT: SUBMITTED - the page now says: "Your post has been shared"'


@tool("_fa_safe", "test: safe", {"x": P("string", "x")})
def _fa_safe(x: str = ""):
    RAN.append(("safe", x))
    return f"did {x}"


@pytest.fixture(autouse=True)
def _clear():
    RAN.clear()


def _run(agent, text, rec=None):
    rec = rec or Recorder()
    return asyncio.run(agent.run(text, rec.emit, rec.confirm))


# ---------------------------------------------------------------- autopilot floor
def test_autopilot_classifier():
    for summary in ('Click "Pay now" on store.example', 'Click "Place order"', 'Click "Subscribe" on x.com'):
        assert autopilot.must_ask("browser_click", "critical", summary, {}) == "a payment"
    assert autopilot.must_ask("browser_click", "critical", "Open checkout", {"url": "https://x.com/checkout"})
    assert autopilot.must_ask("delete_path", "critical", "Move to Recycle Bin: a.txt", {}) is not None
    assert autopilot.must_ask("browser_click", "critical", 'Click "Delete your account" on x.com', {}) is not None
    assert autopilot.must_ask("browser_type_secret", "critical", "type password on evil.example", {}) is not None
    for summary in ('Click "Post" on linkedin.com', 'Click "Submit application"', 'Send email to a@b.com'):
        assert autopilot.must_ask("browser_click", "critical", summary, {}) is None
    assert autopilot.must_ask("browser_click", "critical", 'Click "Pay now"', {}, include_payments=True) is None


def test_full_access_does_everything_but_keeps_the_floor(monkeypatch):
    monkeypatch.setattr(settings, "full_access", True)
    monkeypatch.setattr(settings, "full_access_payments", False)
    llm = FakeLLM([tool_call("_fa_safe", {"x": "1"}, "a"), tool_call("_fa_post", {"x": "hello"}, "b"), reply("done")])
    rec = Recorder()
    _run(agent := Agent(llm=llm, persist=False), "post it", rec)
    assert ("safe", "1") in RAN and ("post", "hello") in RAN           # both ran, no card
    assert rec.requests == []
    assert any(e["type"] == "note" and "Auto-approved" in e.get("text", "") for e in rec.events)

    # a payment still asks, even in full access
    RAN.clear()
    @tool("_fa_pay", "test: pay", {}, risk=CRITICAL, summary=lambda a: 'Click "Pay now" on store.example')
    def _fa_pay():
        RAN.append(("pay", 1))
        return "paid"
    llm = FakeLLM([tool_call("_fa_pay", {}), reply("ok")])
    rec = Recorder(answers=[False])
    _run(Agent(llm=llm, persist=False), "buy it", rec)
    assert [r["tool"] for r in rec.requests] == ["_fa_pay"] and RAN == []   # asked, denied


def test_full_access_payments_floor_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(settings, "full_access", True)
    monkeypatch.setattr(settings, "full_access_payments", True)
    @tool("_fa_pay2", "test: pay", {}, risk=CRITICAL, summary=lambda a: 'Click "Pay now"')
    def _fa_pay2():
        RAN.append("paid")
        return "paid"
    rec = Recorder()
    _run(Agent(llm=FakeLLM([tool_call("_fa_pay2", {}), reply("ok")]), persist=False), "buy it", rec)
    assert RAN == ["paid"] and rec.requests == []


def test_full_access_has_a_per_run_cap(monkeypatch):
    monkeypatch.setattr(settings, "full_access", True)
    monkeypatch.setattr(settings, "full_access_cap", 2)
    script = [tool_call("_fa_post", {"x": str(i)}, f"c{i}") for i in range(4)] + [reply("done")]
    rec = Recorder(answers=[True, True, True, True])
    _run(Agent(llm=FakeLLM(script), persist=False), "post a lot", rec)
    assert len(RAN) == 4 and len(rec.requests) == 2       # first 2 auto, then it starts asking


def test_quality_checks_still_apply_in_full_access(monkeypatch):
    from karya.tools import browser
    monkeypatch.setattr(settings, "full_access", True)
    sess = browser.BrowserSession()
    sess.items = {5: {"id": 5, "tag": "button", "label": "Submit application"}}
    sess.url = "https://jobs.ashbyhq.com/acme/123/application"
    monkeypatch.setattr(sess, "call", lambda fn, *a: fn(*a))
    monkeypatch.setattr(sess, "form_check", lambda eid: {"empty": ["Name", "Email"]})
    monkeypatch.setattr(settings, "browser_mode", "karya")
    monkeypatch.setattr(browser, "session", sess)
    stop = TOOLS["browser_click"].precheck({"element_id": 5})
    assert stop and stop.startswith("NOT CLICKED")        # autopilot doesn't skip the empty-fields check


# ---------------------------------------------------------------- security
def test_scrub_hides_secrets(monkeypatch):
    monkeypatch.setattr(secrets_filter, "_live_secrets", lambda: ["mytok3n-value-xyz"])
    assert secrets_filter.scrub("the key is ksk_abcdef123456 ok") == "the key is ***hidden*** ok"
    assert "mytok3n-value-xyz" not in secrets_filter.scrub("token=mytok3n-value-xyz")
    assert secrets_filter.scrub("GROQ_API_KEY=gsk_abc123def456ghi789") == "GROQ_API_KEY=***hidden***"
    assert secrets_filter.scrub("just normal text") == "just normal text"
    assert secrets_filter.scrub("sk-abcdefghijklmnopqrstuvwx and AKIA1234567890ABCDEF") == "***hidden*** and ***hidden***"


def test_read_file_refuses_secret_files(tmp_path, monkeypatch):
    assert run_tool("read_file", {"path": str(ROOT / ".env")}).startswith("ERROR: Karya won't read")
    assert run_tool("read_file", {"path": "data/vault.json"}).startswith("ERROR: Karya won't read")
    for name in ("id_rsa", "credentials.json", "secrets.yaml"):
        (tmp_path / name).write_text("x", encoding="utf-8")
        assert run_tool("read_file", {"path": str(tmp_path / name)}).startswith("ERROR: Karya won't read"), name
    ok = tmp_path / "notes.txt"
    ok.write_text("hello", encoding="utf-8")
    assert run_tool("read_file", {"path": str(ok)}) == "hello"


def test_write_to_karya_files_needs_approval():
    assert TOOLS["write_file"].assess({"path": str(ROOT / ".env"), "content": "x"})[0] == CRITICAL
    assert TOOLS["write_file"].assess({"path": str(ROOT / "karya" / "agent.py"), "content": "x"})[0] == CRITICAL
    assert TOOLS["write_file"].assess({"path": "note.txt", "content": "x"})[0] == SAFE   # new file in workspace


def test_a_command_that_prints_a_key_is_scrubbed_before_the_model(monkeypatch):
    from karya.tools import pc as pc_mod
    monkeypatch.setattr(pc_mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"KIRO_API_KEY=ksk_leaky123456789",
                                                       "stderr": b""})())
    llm = FakeLLM([tool_call("run_command", {"command": "type .env"}), reply("done")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[True])
    _run(agent, "show env", rec)
    tool_result = [m["content"] for m in agent.history if m.get("role") == "tool"][0]
    assert "ksk_leaky" not in tool_result and "***hidden***" in tool_result


def test_security_check_reports_locked_down():
    report = run_tool("security_check", {})
    data = json.loads(report) if isinstance(report, str) else report
    joined = " ".join(data["secure"])
    assert "127.0.0.1" in joined and "DPAPI" in joined and "cannot read" in joined.lower()
    assert "limits_per_day" in data


# ---------------------------------------------------------------- one login reused
def test_reuse_login_falls_back_to_primary(monkeypatch):
    vault.set_primary("murali@example.com", "MyOnePass!23")
    assert vault.get_secret("some-new-site.com") is None               # reuse off: no fallback
    monkeypatch.setattr(settings, "reuse_login", True)
    assert vault.get_secret("some-new-site.com") == ("murali@example.com", "MyOnePass!23")
    vault.save_account("linkedin.com", "me@work.com", "LinkedInPass!9")
    assert vault.get_secret("linkedin.com") == ("me@work.com", "LinkedInPass!9")   # site-specific wins


def test_make_account_reuses_the_primary_without_asking(monkeypatch):
    vault.set_primary("murali@example.com", "MyOnePass!23")
    monkeypatch.setattr(settings, "reuse_login", True)
    out = accounts_tools.vault_new_password("newportal.com")
    assert "newportal.com" in out and "usual login" in out
    assert vault.get_secret("newportal.com") == ("murali@example.com", "MyOnePass!23")   # same password reused
    monkeypatch.setattr(settings, "reuse_login", False)
    out = accounts_tools.vault_new_password("other.com", "me@x.com")
    assert "strong saved password" in out
    user, pw = vault.get_secret("other.com")
    assert user == "me@x.com" and len(pw) >= 16 and pw != "MyOnePass!23"              # unique generated one


def test_agent_uses_primary_instead_of_asking(monkeypatch):
    vault.set_primary("murali@example.com", "MyOnePass!23")
    monkeypatch.setattr(settings, "reuse_login", True)
    asked = []

    async def ask(request):
        asked.append(request)
        return None
    llm = FakeLLM([tool_call("request_credentials", {"site": "swish.global"}), reply("ok")])
    agent = Agent(llm=llm, persist=False)
    asyncio.run(agent.run("sign me up on swish", Recorder().emit, Recorder().confirm, ask))
    result = [m["content"] for m in agent.history if m.get("role") == "tool"][0]
    assert "primary login" in result and "don't ask" in result and asked == []


def test_saving_the_primary_login(monkeypatch):
    async def ask(request):
        assert request["site"] == "primary"
        return {"username": "murali@example.com", "password": "MyOnePass!23"}
    llm = FakeLLM([tool_call("request_credentials", {"site": "primary"}), reply("saved")])
    agent = Agent(llm=llm, persist=False)
    asyncio.run(agent.run("save my main login", Recorder().emit, Recorder().confirm, ask))
    assert vault.primary() is not None
    result = [m["content"] for m in agent.history if m.get("role") == "tool"][0]
    assert "primary login" in result


# ---------------------------------------------------------------- posting playbooks
def test_how_to_post_instagram_keeps_size_and_audio():
    book = json.loads(_as_json(run_tool("how_to_post", {"platform": "instagram"})))
    steps = " ".join(book["steps"]).lower()
    assert "original" in steps and ("audio" in steps or "sound" in steps) and "don't mute" in steps.lower()
    assert book["login_needed"] is True


def test_how_to_post_x_is_free():
    assert "free" in json.loads(_as_json(run_tool("how_to_post", {"platform": "x"})))["notes"].lower()
    assert "free" in json.loads(_as_json(run_tool("how_to_post", {"platform": "twitter"})))["notes"].lower()  # alias


def test_instagram_compose_opens_the_site_with_the_steps(monkeypatch):
    from karya.tools import browser
    monkeypatch.setattr(browser, "_run", lambda method, *a: f"opened {a[0] if a else ''}")
    out = browser.social_compose("instagram", "my caption")
    assert "instagram.com" in out and "Original" in out and "NEXT" in out


def _as_json(value):
    return value if isinstance(value, str) else json.dumps(value)
