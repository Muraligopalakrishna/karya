"""The real chat page in headless Chrome: the first-run guide, the Setup window and its tabs, and a login card."""
import socket
import sys
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from karya import config, vault
from karya.agent import Agent
from karya.server import create_app

from .conftest import FakeLLM, reply, tool_call

pytestmark = [pytest.mark.browser, pytest.mark.skipif(sys.platform != "win32", reason="vault uses Windows DPAPI")]


def _saved(env, text, timeout=10.0):
    """Skip and Finish close the guide at once and save in the background: wait for the save."""
    end = time.time() + timeout
    while time.time() < end:
        if text in env.read_text(encoding="utf-8"):
            return True
        time.sleep(0.1)
    return False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_guide_setup_tabs_and_login_card(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=\nEMAIL_ADDRESS=\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config.settings, "reload", lambda: config.settings)
    import karya.server as server_mod
    monkeypatch.setattr(server_mod, "_check_providers", lambda llm, names: [
        {"provider": n, "title": n.title(), "ok": True, "model": "openai/gpt-oss-120b", "plan": "free",
         "tokens_per_minute": 8000, "summary": "free/small plan: Karya keeps each request small"} for n in sorted(names)])
    for key in config.EDITABLE_KEYS:
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(config.settings, "providers", [])      # a fresh download: no AI yet
    # Sign-in states, the offline AI and app configs are faked: the test never runs the real CLIs or reads your apps
    from karya import claude_bridge, codex_bridge, connect, kiro_bridge, local_ai, mcp_apps, onboarding
    monkeypatch.setattr(kiro_bridge, "login_status", lambda: {"installed": True, "signed_in": False})
    monkeypatch.setattr(codex_bridge, "login_status", lambda fresh=False: {"installed": False, "signed_in": False})
    monkeypatch.setattr(claude_bridge, "login_status", lambda fresh=False: {"installed": True, "signed_in": False})
    monkeypatch.setattr(local_ai, "status", lambda cached=False: {"installed": True, "running": True, "ready": False,
                                                                  "model": "karya-qwen", "models": []})
    monkeypatch.setattr(connect, "models_for", lambda name, fresh=False: [{"id": "m2", "name": "m2", "description": ""}])
    monkeypatch.setattr(onboarding, "_import_in_background", lambda path: onboarding.IMPORT.update(state="done"))
    added = []
    monkeypatch.setattr(mcp_apps, "overview", lambda root=None: [
        {"id": "cursor", "name": "Cursor", "installed": True, "file": "x", "state": "connected" if added else "no"},
        {"id": "windsurf", "name": "Windsurf", "installed": False, "file": "y", "state": "no"}])
    monkeypatch.setattr(mcp_apps, "connect", lambda app_id, root=None: added.append(app_id) or {
        "id": app_id, "name": "Cursor", "state": "connected", "next": "Restart Cursor so it loads Karya's tools."})
    resume = tmp_path / "Asha Rao.pdf"
    resume.write_bytes(b"%PDF-1.4 Asha Rao, product analyst")
    port = _free_port()
    llm = FakeLLM([tool_call("request_credentials", {"site": "linkedin.com", "reason": "to use Easy Apply"}),
                   reply("Logged in and ready.")])
    app = create_app(agent=Agent(llm=llm, persist=False), token="ui-token", port=port)
    app.state.hub.make_bot = lambda record: Agent(        # a bot with its own scripted AI
        llm=FakeLLM([reply("Found 3 PM jobs in Pune: Acme, Globex, Initech.\nRemember: covered Acme")]),
        persist=False, bot=record)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/?token=ui-token")
            page.wait_for_selector("#conn-dot.on", timeout=15000)

            # a fresh download: the welcome guide opens by itself; it can be skipped
            page.wait_for_selector("#guide .guide-card", timeout=10000)
            assert "Welcome to Karya" in page.inner_text("#guide")
            assert page.inner_text("#guide .guide-next") == "Skip for now"          # no AI yet
            page.click("#guide button:has-text('Skip setup')")
            page.wait_for_selector("#guide", state="hidden", timeout=5000)
            assert _saved(env, "KARYA_ONBOARDED=skipped")
            page.wait_for_selector("#notice:has-text('no AI to think with')", timeout=5000)
            assert page.inner_text("#provider-label") == "No AI connected"
            assert page.locator(".idea").count() == 6

            # the agent asks for a login -> secure card in the chat (done before any AI is connected: connecting
            # one switches the agent to the real AI client, as it should in normal use)
            page.fill("#input", "apply with easy apply")
            page.keyboard.press("Enter")
            page.wait_for_selector("form.ask", timeout=15000)
            assert "linkedin.com" in page.inner_text("form.ask") and "Easy Apply" in page.inner_text("form.ask")
            page.locator("form.ask input").nth(0).fill("asha@example.com")
            page.locator("form.ask input[type=password]").fill("Li#pass2")
            page.click("form.ask .approve")
            page.wait_for_selector(".msg.assistant:has-text('Logged in and ready.')", timeout=15000)
            assert vault.get_secret("linkedin.com") == ("asha@example.com", "Li#pass2")
            assert "Li#pass2" not in page.content()

            # Setup is one window with tabs; the AI tab shows every way to connect one
            page.click("#setup-btn")
            page.wait_for_selector("#setup-panel .setup-card", timeout=5000)
            assert page.get_attribute("#tab-ai", "aria-selected") == "true"
            view = page.locator("#view-ai")
            page.wait_for_selector("#view-ai .option h4:has-text('Claude')", timeout=5000)
            for text in ("Paste an AI key", "Kiro", "ChatGPT", "Free, on this PC"):
                assert view.locator(f".option h4:has-text('{text}')").count() == 1
            page.wait_for_selector("#view-ai button:has-text('Sign in with Claude')", timeout=5000)
            assert view.locator("button:has-text('Google')").count() == 1                 # Kiro sign-in
            assert "npm install -g @openai/codex" in view.inner_text()                    # Codex not installed
            assert view.locator("button:has-text('Download the free model')").count() == 1
            # Escape closes it; "Safety & more" opens the guide again
            page.keyboard.press("Escape")
            page.wait_for_selector("#setup-panel", state="hidden", timeout=5000)
            page.click("#setup-btn")
            page.click("#tab-more")
            page.click("#view-more button:has-text('Show the welcome guide again')")
            page.wait_for_selector("#guide .guide-card", timeout=5000)
            assert page.is_hidden("#setup-panel")

            # guide step 1: paste any key; Karya works out the service, saves it and checks it
            page.fill("#guide [aria-label='Paste an AI key']", "gsk_" + "p" * 40)
            page.click("#guide .key-form button")
            page.wait_for_selector("#guide .form-msg:has-text('Groq connected')", timeout=10000)
            assert "GROQ_API_KEY=gsk_" + "p" * 40 in env.read_text(encoding="utf-8")
            assert page.input_value("#guide [aria-label='Paste an AI key']") == ""
            page.click("#guide .guide-next")
            # step 2: about you, and the resume (uploaded from the page)
            page.wait_for_selector("#guide [aria-label='Your name']", state="visible", timeout=5000)
            page.fill("#guide [aria-label='Your name']", "Asha Rao")
            page.fill("#guide [aria-label='Your email']", "asha@example.com")
            page.fill("#guide [aria-label='City and country']", "Pune, India")
            page.set_input_files("#guide [aria-label='Your resume file']", str(resume))
            page.wait_for_selector("#guide .resume-state:has-text('Karya read your resume')", timeout=10000)
            assert f"RESUME_PATH={onboarding.RESUME_DIR / 'Asha Rao.pdf'}" in env.read_text(encoding="utf-8")
            page.click("#guide .guide-next")
            page.wait_for_selector("#guide button:has-text('Connect WhatsApp')", state="visible", timeout=5000)
            from karya.memory import memory_store
            assert memory_store.load()["profile"]["name"] == "Asha Rao"
            # step 3: extras, then Finish
            assert page.inner_text("#guide .guide-next") == "Finish"
            page.click("#guide .guide-next")
            page.wait_for_selector("#guide", state="hidden", timeout=5000)
            assert _saved(env, "KARYA_ONBOARDED=true")

            # Setup > AI: a key that fits two services asks which one
            page.click("#setup-btn")
            page.click("#tab-ai")
            page.fill("#view-ai [aria-label='Paste an AI key']", "sk-" + "0a" * 16)
            page.click("#view-ai .key-form button")
            page.wait_for_selector("#view-ai .choose-service button:has-text('DeepSeek')", timeout=10000)
            page.click("#view-ai .choose-service button:has-text('DeepSeek')")
            page.wait_for_selector("#view-ai .form-msg:has-text('DeepSeek connected')", timeout=10000)
            # every service one by one (advanced), saved and checked
            page.click("#view-ai summary:has-text('Every AI service, one by one')")
            page.wait_for_selector("#view-ai [aria-label='Groq API key']", timeout=10000)
            assert page.locator("#view-ai .providers .field").count() >= 12
            page.fill("#view-ai [aria-label='Groq API key']", "gsk_from_the_form")
            page.click("#view-ai .providers ~ .form-actions button[type=submit]")
            page.wait_for_selector("#view-ai .form-msg:has-text('works with')", timeout=10000)
            assert "GROQ_API_KEY=gsk_from_the_form" in env.read_text(encoding="utf-8")
            assert page.input_value("#view-ai [aria-label='Groq API key']") == ""       # secret not shown back

            # About you: the Gmail address for sending
            page.click("#tab-you")
            page.fill("#view-you [aria-label='Gmail address (to send/read email)']", "asha@example.com")
            page.click("#view-you form.settings button[type=submit]")
            page.wait_for_selector("#view-you .form-msg:has-text('Saved')", timeout=10000)
            assert "EMAIL_ADDRESS=asha@example.com" in env.read_text(encoding="utf-8")

            # Other AI apps: one click adds Karya to an installed app
            page.click("#tab-apps")
            assert "Two ways to use Karya with other AI apps" in page.inner_text("#view-apps")
            page.wait_for_selector(".mcp-apps li:has-text('Cursor') button", timeout=10000)
            assert page.locator(".mcp-apps li", has_text="Windsurf").count() == 0             # not installed
            page.click(".mcp-apps li:has-text('Cursor') button")
            page.wait_for_selector(".mcp-box .form-msg:has-text('Restart Cursor')", timeout=10000)
            page.wait_for_selector(".mcp-apps li:has-text('Karya added')", timeout=10000)

            # Browser & logins: a saved login; the password is never shown back
            page.click("#tab-browser")
            form = page.locator(".account-form")
            form.locator("input").nth(0).fill("github.com")
            form.locator("input").nth(1).fill("asha")
            form.locator("input").nth(2).fill("Gh#pass1")
            form.locator("button").click()
            page.wait_for_selector(".account-list li:has-text('github.com')", timeout=10000)
            assert page.locator(".account-list li", has_text="linkedin.com").count() == 1   # the one saved from chat
            assert "Gh#pass1" not in page.content()
            assert vault.get_secret("github.com") == ("asha", "Gh#pass1")

            # Phone & bots: make a bot, then "@Maya ..." in the chat goes straight to it; the chat stays free
            page.click("#tab-phone")
            assert "Not linked yet." in page.inner_text(".phone-box .phone-state")
            assert "No bots yet." in page.inner_text(".agents-box")
            page.fill("[aria-label='Bot name']", "Maya")
            page.fill("[aria-label='What the bot does']", "find product manager jobs in India and apply")
            page.click(".make-bot button[type=submit]")
            page.wait_for_selector(".agent-list li:has-text('Maya')", timeout=10000)
            assert "works when you give it a task" in page.inner_text(".agent-list")
            page.click("#setup-panel button[aria-label='Close Setup']")
            page.wait_for_selector("#setup-panel", state="hidden", timeout=5000)
            page.fill("#input", "@Maya find PM jobs in Pune")
            page.keyboard.press("Enter")
            page.wait_for_selector(".msg.assistant:has-text('Maya is on it')", timeout=10000)
            page.wait_for_selector(".bot-report:has-text('Found 3 PM jobs in Pune')", timeout=15000)
            assert "from Maya" in page.inner_text(".bot-report")
            assert "Maya finished" in page.inner_text(".bot-card")
            assert page.is_enabled("#send-btn")

            # Safety & more: Karya in the Start menu (the test's own folder), then Quit Karya
            from karya import shortcuts
            page.click("#setup-btn")
            page.click("#tab-more")
            page.wait_for_selector("#view-more h3:has-text('The Karya app')", timeout=5000)
            page.check("#view-more [aria-label^='In the Start menu']")
            for _ in range(60):
                if shortcuts.path_for("start_menu").exists():
                    break
                time.sleep(0.25)
            assert shortcuts.path_for("start_menu").exists() and str(tmp_path) in str(shortcuts.path_for("start_menu"))
            quits = []
            app.state.quit = lambda: quits.append(True)
            page.wait_for_function("() => document.querySelector(\"#view-more [aria-label^='In the Start menu']\").checked")
            page.click("#view-more button:has-text('Quit Karya')")
            page.wait_for_selector("#guide:has-text('Karya has stopped')", timeout=5000)
            assert "type Karya" in page.inner_text("#guide")
            time.sleep(1)
            assert quits == [True]
            browser.close()
    finally:
        server.should_exit = True
    assert errors == []
