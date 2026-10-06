"""Clicks through the Setup panel and the secure login card in headless Chrome (real server, scripted LLM)."""
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


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_setup_panel_and_login_card(tmp_path, monkeypatch):
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
    port = _free_port()
    llm = FakeLLM([tool_call("request_credentials", {"site": "linkedin.com", "reason": "to use Easy Apply"}),
                   reply("Logged in and ready.")])
    app = create_app(agent=Agent(llm=llm, persist=False), token="ui-token", port=port)
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
            page = browser.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{port}/?token=ui-token")
            page.wait_for_selector("#conn-dot.on", timeout=15000)

            # the agent asks for a login -> secure card in the chat (done first: saving settings below
            # switches the agent to the real AI client, as it should in normal use)
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

            page.wait_for_selector("#setup-panel .settings .field", timeout=10000, state="attached")
            if page.is_hidden("#setup-panel"):
                page.click("#setup-btn")
            page.wait_for_selector("[aria-label='Groq API key']", timeout=10000)
            assert page.locator("#setup-panel .providers .field").count() >= 12   # every provider + custom + order
            for name in ("OpenAI", "Anthropic Claude", "Google Gemini", "Groq"):
                assert page.locator(f"[aria-label='{name} API key']").count() == 1

            # save an API key + email from the form; the key is checked and its plan shown
            page.fill("[aria-label='Groq API key']", "gsk_from_the_form")
            page.fill("[aria-label='Gmail address (to send/read email)']", "asha@example.com")
            page.click(".settings button[type=submit]")
            page.wait_for_selector(".form-msg:has-text('works with')", timeout=10000)
            assert "free/small plan" in page.inner_text(".form-msg")
            saved = env.read_text(encoding="utf-8")
            assert "GROQ_API_KEY=gsk_from_the_form" in saved and "EMAIL_ADDRESS=asha@example.com" in saved
            assert page.input_value("[aria-label='Groq API key']") == ""  # secret not shown back

            # add an account in the Accounts box; the password is never shown back
            form = page.locator(".account-form")
            form.locator("input").nth(0).fill("github.com")
            form.locator("input").nth(1).fill("asha")
            form.locator("input").nth(2).fill("Gh#pass1")
            form.locator("button").click()
            page.wait_for_selector(".account-list li:has-text('github.com')", timeout=10000)
            assert page.locator(".account-list li", has_text="linkedin.com").count() == 1   # the one saved from chat
            assert "Gh#pass1" not in page.content()
            assert vault.get_secret("github.com") == ("asha", "Gh#pass1")
            browser.close()
    finally:
        server.should_exit = True
    assert errors == []
