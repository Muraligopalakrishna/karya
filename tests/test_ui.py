"""Drives the real chat page in headless Chrome against a real server (scripted LLM, no AI needed)."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from karya.agent import Agent
from karya.server import create_app

from .conftest import FakeLLM, reply, tool_call
from .test_agent import RAN

pytestmark = pytest.mark.browser

EVIL = {
    "img": "<img src=x onerror=alert(1)>",
    "script": "<script>alert(1)</script>",
    "js_link": "[click](javascript:alert(1))",
    "attr_break": '[x](https://ok.com/"onmouseover="alert(1))',
    "bare_url": 'visit https://example.com/?a=1&b="2" now',
}
ANSWER = "**Done.** Posted it.\n\n| Job | Company |\n|---|---|\n| Frontend | Acme |\n\n- one\n- two\n\n<img src=x onerror=alert(1)>"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    RAN.clear()
    port = _free_port()
    llm = FakeLLM([tool_call("_test_critical", {"x": "linkedin post"}), reply(ANSWER),
                   tool_call("_test_critical", {"x": "second"}), reply("Okay, I won't do that.")])
    agent = Agent(llm=llm, persist=False)
    app = create_app(agent=agent, token="ui-test-token", port=port)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port, agent
    srv.should_exit = True
    thread.join(timeout=5)


def test_chat_page_end_to_end(server):
    port, agent = server
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        page.goto(f"http://127.0.0.1:{port}/?token=ui-test-token")
        page.wait_for_selector("#conn-dot.on", timeout=15000)
        assert page.url == f"http://127.0.0.1:{port}/"          # token removed from the address bar
        assert page.locator(".chip").count() == 6
        assert "brain:" in page.inner_text("#provider-label")

        # the markdown renderer must never produce live HTML from model/web text
        results = page.evaluate("""(cases) => {
            const d = document.createElement('div'); const out = {};
            for (const [k, v] of Object.entries(cases)) {
              d.innerHTML = window.KaryaMarkdown(v);
              out[k] = {img: d.querySelectorAll('img,script').length,
                        on: [...d.querySelectorAll('*')].filter(e => [...e.attributes].some(a => a.name.startsWith('on'))).length,
                        badHref: [...d.querySelectorAll('a')].filter(a => !/^https?:\\/\\//.test(a.getAttribute('href'))).length,
                        links: d.querySelectorAll('a').length};
            }
            return out; }""", EVIL)
        for name, r in results.items():
            assert r["img"] == 0 and r["on"] == 0 and r["badHref"] == 0, (name, r)
        assert results["bare_url"]["links"] == 1

        # a chat turn with an approval card -> Approve
        page.fill("#input", "post this on linkedin")
        page.keyboard.press("Enter")
        page.wait_for_selector(".confirm .approve", timeout=15000)
        assert "linkedin post" in page.inner_text(".confirm")
        assert page.is_visible("#stop-btn")
        page.click(".confirm .approve")
        page.wait_for_selector(".msg.assistant table", timeout=15000)
        bubble = page.locator(".msg.assistant .bubble").last
        assert bubble.locator("strong").inner_text() == "Done."
        assert bubble.locator("li").count() == 2
        assert bubble.locator("img").count() == 0 and "<img" in bubble.inner_text()  # shown as text, not HTML
        assert RAN == [("critical", "linkedin post")]
        page.wait_for_selector("#stop-btn", state="hidden", timeout=10000)

        # second turn -> Deny: the tool must not run
        page.fill("#input", "do another one")
        page.keyboard.press("Enter")
        page.wait_for_selector(".confirm:not(:has(.result)) .deny", timeout=15000)
        page.click(".confirm:not(:has(.result)) .deny")
        page.wait_for_selector(".msg.assistant:nth-child(n) >> text=won't", timeout=15000)
        assert page.locator(".msg.assistant").count() == 2
        assert "won't" in page.locator(".msg.assistant .bubble").last.inner_text()
        assert RAN == [("critical", "linkedin post")]
        page.wait_for_selector("#stop-btn", state="hidden", timeout=10000)

        # reload keeps the conversation (token remembered, no ?token in the URL)
        page.goto(f"http://127.0.0.1:{port}/")
        page.wait_for_selector("#conn-dot.on", timeout=15000)
        page.wait_for_selector(".msg.user", timeout=10000)
        assert page.locator(".msg.user").count() == 2

        # New chat clears everything
        page.click("#new-chat")
        page.wait_for_selector(".msg", state="detached", timeout=10000)
        assert page.locator(".msg").count() == 0
        assert agent.history == []
        assert dialogs == ["Start a new chat? The current conversation will be cleared."]  # no alert() ever fired
        browser.close()
