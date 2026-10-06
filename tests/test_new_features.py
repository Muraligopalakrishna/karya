import asyncio
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from karya import config, registry, vault
from karya.agent import Agent, trim_messages
from karya.registry import CRITICAL, SAFE, TOOLS, run_tool
from karya.tools import browser, jobs, resume, web

from .conftest import FakeLLM, Recorder, reply, tool_call

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="vault uses Windows DPAPI")


# ------------------------------------------------------------- keeping the conversation (the "what do you mean?" bug)
def _turn(text, steps=3, size=4000, answer="Here are the results."):
    msgs = [{"role": "user", "content": text}]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"{text[:5]}{i}", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"{text[:5]}{i}", "content": "R" * size})
    msgs.append({"role": "assistant", "content": answer})
    return msgs


def test_trim_keeps_earlier_requests_on_small_budgets():
    system = {"role": "system", "content": "S" * 3500}
    history = (_turn("i want to sell my site mysite.com and email potential buyers")
               + _turn("not these, find trading education companies outside India")
               + [{"role": "user", "content": "find buyers and their emails and email them all"}])
    out = trim_messages([system] + history, 9_000)
    users = [m["content"] for m in out if m["role"] == "user"]
    assert any("mysite.com" in u for u in users), users          # first request still visible
    assert users[-1].startswith("find buyers")
    assert all(m["role"] != "tool" for m in out[:-1])                  # old tool output condensed away
    notes = [m["content"] for m in out if m["role"] == "assistant"]
    assert any("tools I used for this: web_search" in n for n in notes)
    assert sum(len(m.get("content") or "") for m in out) <= 9_000


def test_trim_lists_dropped_requests_in_system_prompt():
    system = {"role": "system", "content": "S" * 3000}
    history = []
    for i in range(30):
        history += _turn(f"request number {i} about topic {i}", steps=1, size=500, answer="A" * 900)
    history.append({"role": "user", "content": "and now?"})
    out = trim_messages([system] + history, 8_000)
    assert "Earlier requests in this chat" in out[0]["content"]
    assert out[-1]["content"] == "and now?"
    assert sum(len(m.get("content") or "") for m in out) <= 8_000


def test_tool_groups_do_not_pile_up_across_turns():
    llm = FakeLLM([tool_call("stock_quote", {"symbols": ["TCS.NS"]}), reply("TCS is 3,000"), reply("hello")],
                  provider_name="groq")
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    asyncio.run(agent.run("tcs share price", rec.emit, rec.confirm))
    assert agent.recent_groups == {"finance"}
    asyncio.run(agent.run("thanks!", rec.emit, rec.confirm))
    assert agent.recent_groups == set()                                # nothing used -> nothing carried over


# ------------------------------------------------------------- vault + secure logins
@windows_only
def test_vault_encrypts_and_matches_sites():
    vault.save_account("https://www.linkedin.com/login", "me@example.com", "S3cret!pass")
    raw = vault.VAULT_FILE.read_text(encoding="utf-8")
    assert "S3cret!pass" not in raw and "linkedin.com" in raw
    assert vault.get_secret("in.linkedin.com") == ("me@example.com", "S3cret!pass")
    assert vault.list_accounts() == [{"site": "linkedin.com", "username": "me@example.com", "has_password": True,
                                      "notes": "", "updated": vault.list_accounts()[0]["updated"]}]
    assert "S3cret" not in run_tool("list_accounts", {})
    assert vault.site_matches("x.com", "twitter.com") and not vault.site_matches("linkedin.com", "linkedin.com.evil.io")
    pw = vault.generate_password()
    assert len(pw) == 20 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw)
    assert "Saved" in run_tool("vault_new_password", {"site": "workatastartup.com", "username": "me@example.com"})
    assert "already exists" in run_tool("vault_new_password", {"site": "workatastartup.com", "username": "x"})


@windows_only
def test_secret_typing_only_on_the_matching_site():
    vault.save_account("linkedin.com", "me@example.com", "pw")
    browser.session.url = "https://www.linkedin.com/login"
    assert TOOLS["browser_type_secret"].assess({"element_id": 3, "site": "linkedin.com"})[0] == SAFE
    browser.session.url = "https://linkedin-login.example.net/"
    level, text = TOOLS["browser_type_secret"].assess({"element_id": 3, "site": "linkedin.com"})
    assert level == CRITICAL and "DIFFERENT site" in text


@windows_only
def test_request_credentials_never_reaches_the_model():
    llm = FakeLLM([tool_call("request_credentials", {"site": "upwork.com", "reason": "to bid on a project"}), reply("done")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    asked = []

    async def ask(req):
        asked.append(req)
        return {"username": "me@example.com", "password": "TopSecret#1"}

    asyncio.run(agent.run("bid on upwork", rec.emit, rec.confirm, ask))
    assert asked[0]["site"] == "upwork.com" and "bid" in asked[0]["reason"]
    assert vault.get_secret("upwork.com") == ("me@example.com", "TopSecret#1")
    assert "TopSecret" not in json.dumps(agent.history) and "TopSecret" not in json.dumps(llm.calls)
    assert "TopSecret" not in json.dumps(rec.events)


# ------------------------------------------------------------- resume: build + tailor with approval for new skills
MASTER = {"name": "Asha Rao", "headline": "Frontend developer", "email": "asha@example.com", "skills": ["HTML", "CSS", "React"],
          "experience": [{"title": "Frontend Intern", "company": "Acme", "start": "2024", "end": "2025",
                          "bullets": ["Built responsive pages in React"]}],
          "education": [{"degree": "B.Tech CS", "school": "Some University"}]}


def test_resume_new_skills_need_approval():
    assert json.loads(run_tool("get_resume_data", {}))["exists"] is False
    assert TOOLS["save_resume_data"].assess({"resume": MASTER})[0] == SAFE        # first save: nothing to compare
    run_tool("save_resume_data", {"resume": MASTER})
    tailored = dict(MASTER, skills=["React", "CSS", "HTML"], summary="Frontend developer for fintech UIs")
    assert TOOLS["build_resume"].assess({"resume": tailored})[0] == SAFE           # reordered, nothing new
    padded = dict(MASTER, skills=["React", "TypeScript", "GraphQL"])
    level, text = TOOLS["build_resume"].assess({"resume": padded, "job_title": "Frontend Engineer"})
    assert level == CRITICAL and "TypeScript" in text and "GraphQL" in text and "React" not in text.split(":", 1)[1]


@pytest.mark.browser
def test_build_resume_pdf_and_learn_approved_skills(isolated):
    run_tool("save_resume_data", {"resume": MASTER})
    out = json.loads(run_tool("build_resume", {"resume": dict(MASTER, skills=["React", "TypeScript"]),
                                               "job_title": "Frontend Engineer", "company": "Vercel"}))
    assert out["pdf"].endswith(".pdf") and os.path.getsize(out["pdf"]) > 5_000 and "Vercel" in out["pdf"]
    assert out["added_skills"] == ["TypeScript"]
    assert "TypeScript" in resume.load_master()["skills"]                           # approved -> remembered


# ------------------------------------------------------------- job ranking (offline fixtures)
def _fake_sources(monkeypatch):
    rows = [
        {"source": "YC", "title": "Frontend Engineer", "company": "Acme AI", "location": "Remote", "experience": "Any (new grads ok)",
         "skills_text": "React TypeScript", "posted": "2026-09-28T10:00:00Z", "url": "https://yc/1"},
        {"source": "YC", "title": "Senior Staff Frontend Engineer", "company": "BigCo", "location": "Remote",
         "posted": "2026-09-28T10:00:00Z", "url": "https://yc/2"},
        {"source": "stripe careers (Greenhouse)", "title": "Frontend Engineer, New Grad", "company": "Stripe", "location": "London, UK",
         "posted": "2026-09-25T00:00:00Z", "url": "https://gh/3"},
        {"source": "Remotive", "title": "Backend Engineer", "company": "DataCo", "location": "Remote: USA only",
         "posted": "2026-09-27", "url": "https://r/4"},
        {"source": "LinkedIn", "title": "Frontend Engineer", "company": "Acme AI", "location": "Remote",
         "posted": "2026-09-28", "url": "https://li/dup"},
        {"source": "LinkedIn", "title": "UI Developer", "company": "Old Corp", "location": "Berlin, Germany",
         "posted": "2026-01-01", "url": "https://li/old"},
    ]
    from karya.tools import funding, job_sources
    for mod in (jobs, job_sources, funding):
        for name in [n for n in dir(mod) if n.startswith("src_")]:
            monkeypatch.setattr(mod, name, lambda *a, **k: [])
    monkeypatch.setattr(jobs, "src_yc", lambda *a, **k: rows)
    monkeypatch.setattr(jobs, "user_skills", lambda: {"react", "css", "html", "typescript"})


def test_find_jobs_ranks_against_preferences(monkeypatch):
    _fake_sources(monkeypatch)
    run_tool("set_job_preferences", {"roles": ["frontend engineer"], "locations": ["United Kingdom", "Remote"], "level": "entry"})
    out = json.loads(run_tool("find_jobs", {"posted_within_days": 30}))
    titles = [(j["company"], j["title"]) for j in out["jobs"]]
    assert titles[0] == ("Acme AI", "Frontend Engineer")                  # skills + entry-friendly + remote + fresh
    assert ("Stripe", "Frontend Engineer, New Grad") in titles             # London matches "United Kingdom"
    assert ("Old Corp", "UI Developer") not in titles                      # Berlin isn't wanted (and it's old)
    assert sum(1 for c, t in titles if c == "Acme AI") == 1               # LinkedIn duplicate removed
    senior = next(j for j in out["jobs"] if j["company"] == "BigCo")
    assert senior["match"] < out["jobs"][0]["match"] and "senior role" in senior["why"]
    usa = next((j for j in out["jobs"] if j["company"] == "DataCo"), None)
    assert usa is None or "limited to US" in usa["why"]


def test_location_fit_any_country():
    assert jobs._location_fit({"location": "San Francisco, CA"}, ["United States"], True)[2]
    assert jobs._location_fit({"location": "Bengaluru, India"}, ["India"], False)[2]
    assert not jobs._location_fit({"location": "Paris, France"}, ["United States", "India"], False)[2]
    pts, why, ok = jobs._location_fit({"location": "Remote (US only)"}, ["India", "Remote"], True)
    assert ok and "limited" in why


def test_application_profile_lists_missing_fields():
    run_tool("update_profile", {"field": "name", "value": "Asha Rao"})
    run_tool("update_profile", {"field": "email", "value": "asha@example.com"})
    prof = json.loads(run_tool("get_application_profile", {}))
    assert prof["first_name"] == "Asha" and prof["last_name"] == "Rao"
    assert "linkedin" in prof["missing"] and "email" not in prof["missing"]


# ------------------------------------------------------------- contacts + search fallback
def test_contacts_from_html_decodes_hidden_emails():
    hidden = "0b" + "".join(f"{ord(c) ^ 0x0b:02x}" for c in "hello@acme-trading.com")   # Cloudflare's encoding
    page = f"""<html><body>
      <a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="{hidden}">[email protected]</a>
      <a href="mailto:partners@acme-trading.com?subject=hi">Partner with us</a> sales@acme-trading.com name@example.com
      <img src="logo@2x.png"> <a href="https://twitter.com/acme">X</a> <a href="https://www.linkedin.com/company/acme/">in</a>
      <a href="/contact-us">Contact</a></body></html>"""
    emails, socials, pages = web.contacts_from_html(page, "https://acme-trading.com/")
    assert emails == {"hello@acme-trading.com", "partners@acme-trading.com", "sales@acme-trading.com"}
    assert socials["x"].startswith("https://twitter.com/acme") and "linkedin" in socials
    assert "https://acme-trading.com/contact-us" in pages


def test_web_search_falls_back_to_other_engines(monkeypatch):
    tried = []

    class FakeDDGS:
        def __init__(self, timeout=10):
            pass

        def text(self, query, backend="auto", **kw):
            tried.append(backend)
            if backend in ("duckduckgo", "yahoo"):
                raise Exception("No results found.")
            return [{"title": "Found", "href": "https://found.example", "body": "ok"}]

    monkeypatch.setattr(web, "DDGS", FakeDDGS)
    rows = json.loads(run_tool("web_search", {"query": "anything"}))
    assert rows[0]["url"] == "https://found.example" and tried[:3] == ["duckduckgo", "yahoo", "google"]


# ------------------------------------------------------------- settings + accounts API
@windows_only
def test_settings_and_accounts_api(tmp_path, monkeypatch):
    from karya.server import create_app
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=\nEMAIL_ADDRESS=\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config.settings, "reload", lambda: config.settings)
    for key in config.EDITABLE_KEYS:
        monkeypatch.setenv(key, "")
    client = TestClient(create_app(agent=Agent(llm=FakeLLM([]), persist=False), token="t0k3n", port=8765,
                                   extra_hosts={"testserver"}))
    import karya.server as server_mod
    monkeypatch.setattr(server_mod, "_check_providers", lambda llm, names: [
        {"provider": n, "ok": True, "model": "m", "plan": "free", "summary": "free/small plan"} for n in sorted(names)])
    assert client.get("/api/settings").status_code == 401
    r = client.post("/api/settings?token=t0k3n", json={"GROQ_API_KEY": "gsk_secret_value", "EMAIL_ADDRESS": "me@example.com"},
                    headers={"origin": "http://testserver"})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"GROQ_API_KEY", "EMAIL_ADDRESS"}
    assert r.json()["checks"] == [{"provider": "groq", "ok": True, "model": "m", "plan": "free", "summary": "free/small plan"}]
    assert "GROQ_API_KEY=gsk_secret_value" in env.read_text(encoding="utf-8")
    data = client.get("/api/settings?token=t0k3n").json()
    view = data["values"]
    assert view["GROQ_API_KEY"] == {"set": True, "value": ""} and view["EMAIL_ADDRESS"]["value"] == "me@example.com"
    assert {"openai", "anthropic", "gemini", "groq", "openrouter"} <= {p["name"] for p in data["providers"]}
    assert client.post("/api/settings?token=t0k3n", json={"PORT": "1"}).status_code == 400          # not editable
    assert client.post("/api/settings?token=t0k3n", json={"GROQ_API_KEY": "x"},
                       headers={"origin": "https://evil.example"}).status_code == 401              # other websites blocked
    assert client.post("/api/accounts?token=t0k3n", json={"site": "github.com", "username": "asha", "password": "pw!"}).status_code == 200
    accounts = client.get("/api/accounts?token=t0k3n").json()["accounts"]
    assert accounts[0]["site"] == "github.com" and "pw!" not in json.dumps(accounts)
    assert client.delete("/api/accounts?token=t0k3n&site=github.com").json() == {"deleted": True}


@windows_only
def test_login_form_over_websocket():
    from karya.server import create_app
    agent = Agent(llm=FakeLLM([tool_call("request_credentials", {"site": "linkedin.com", "reason": "Easy Apply"}),
                               reply("logged in")]), persist=False)
    client = TestClient(create_app(agent=agent, token="t0k3n", port=8765, extra_hosts={"testserver"}))
    with client.websocket_connect("/ws?token=t0k3n", headers={"origin": "http://testserver"}) as ws:
        ws.receive_json()
        ws.send_json({"type": "chat", "text": "apply on linkedin"})
        while True:
            ev = ws.receive_json()
            if ev["type"] == "ask":
                break
        assert ev["site"] == "linkedin.com"
        ws.send_json({"type": "ask_reply", "id": ev["id"], "username": "asha@example.com", "password": "Pw#123"})
        while ws.receive_json()["type"] != "assistant":
            pass
    assert vault.get_secret("linkedin.com") == ("asha@example.com", "Pw#123")
    assert "Pw#123" not in json.dumps(agent.history)



def test_required_years_and_ranking():
    assert jobs.required_years("Overall experience of 3+ yrs with at least 1+ yrs in product management") == 3
    assert jobs.required_years("2-4 years of experience in B2C products") == 2
    assert jobs.required_years("Minimum of 5 years in sales") == 5
    assert jobs.required_years("We were founded 10 years ago. Freshers welcome!") is None
    text = jobs.job_text("About us. " * 100 + "You have 4+ years of product experience.")
    assert "4+ years" in text and len(text) < 1300
    entry = {"level": "entry", "locations": ["India"]}
    pm1 = {"title": "Product Manager I", "location": "Bangalore, India", "text": "Overall experience of 3+ yrs", "source": "x"}
    apm = {"title": "Associate Product Manager", "location": "Bengaluru, India", "text": "Freshers welcome", "source": "x"}
    s_pm1, why_pm1 = jobs.score_job(pm1, [["product", "manager"]], set(), entry)
    s_apm, why_apm = jobs.score_job(apm, [["product", "manager"]], set(), entry)
    assert s_apm > s_pm1 and "needs 3+ yrs" in why_pm1 and "entry-level friendly" in why_apm



def test_apply_buttons_that_only_open_a_form():
    from karya.tools.browser import classify_click
    from karya.registry import CONFIRM
    assert classify_click({"tag": "button"}, "Easy Apply", "https://www.linkedin.com/jobs/view/123") == CONFIRM
    assert classify_click({"tag": "a"}, "Apply for this job", "https://jobs.lever.co/zeta/abc") == CONFIRM
    assert classify_click({"tag": "button"}, "Submit application", "https://www.linkedin.com/jobs/view/123") == CRITICAL
    assert classify_click({"tag": "button"}, "Apply", "https://www.naukri.com/job/123") == CRITICAL  # one-click apply sites


def test_job_ids_and_pick_list(monkeypatch):
    _fake_sources(monkeypatch)
    run_tool("set_job_preferences", {"roles": ["frontend engineer"], "locations": ["United Kingdom", "Remote"], "level": "entry"})
    out = json.loads(run_tool("find_jobs", {"posted_within_days": 30}))
    assert out["jobs"][0]["id"] == "J1" and jobs.job_by_id("j1")["url"] == out["jobs"][0]["url"]
    assert jobs.resolve_job_url("J1") == out["jobs"][0]["url"]
    llm = FakeLLM([tool_call("choose_jobs", {"note": "best matches"}), reply("applying to J1")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    shown = []

    async def ask(req):
        shown.append(req)
        return {"picked": ["J1"], "skip_companies": ["Stripe"]}

    asyncio.run(agent.run("find frontend jobs and let me pick", rec.emit, rec.confirm, ask))
    assert shown[0]["kind"] == "jobs" and shown[0]["jobs"][0]["id"] == "J1" and shown[0]["note"] == "best matches"
    result = json.loads([m for m in agent.history if m["role"] == "tool"][0]["content"])
    assert [p["id"] for p in result["picked"]] == ["J1"] and result["skipped_companies"] == ["Stripe"]
    assert "Stripe" in jobs.job_preferences()["exclude"]
    again = json.loads(run_tool("find_jobs", {"posted_within_days": 30}))
    assert all(j["company"] != "Stripe" for j in again["jobs"])            # skipped companies stay hidden
