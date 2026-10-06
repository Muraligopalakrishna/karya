import json

import pytest

from karya import kiro_bridge
from karya.config import Provider
from karya.llm import LLMClient, LLMError

TOOLS = [{"type": "function", "function": {"name": "stock_quote", "description": "Current price of stocks.",
                                           "parameters": {"type": "object", "required": ["symbols"], "properties": {
                                               "symbols": {"type": "array", "items": {"type": "string"}, "description": "Tickers"}}}}},
         {"type": "function", "function": {"name": "browser_click", "description": "Click an element.",
                                           "parameters": {"type": "object", "properties": {
                                               "element_id": {"type": "integer", "description": "id"},
                                               "direction": {"type": "string", "enum": ["up", "down"]}}}}}]


def test_render_prompt_contains_everything():
    msgs = [{"role": "system", "content": "You are Karya."},
            {"role": "user", "content": "TCS price?"},
            {"role": "assistant", "content": "Checking.", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "stock_quote", "arguments": '{"symbols": ["TCS.NS"]}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"price": 3000}'}]
    text = kiro_bridge.render_prompt(msgs, TOOLS)
    assert "You are Karya." in text and "### USER\nTCS price?" in text
    assert '<tool_call>{"name": "stock_quote", "arguments": {"symbols": ["TCS.NS"]}}</tool_call>' in text
    assert "### RESULT OF stock_quote\n{\"price\": 3000}" in text
    assert "symbols* (list of string)" in text and "direction (up|down)" in text
    assert "<tool_call>" in text.split("=== YOUR NEXT STEP ===")[1]
    assert "YOUR ANSWER" in kiro_bridge.render_prompt([{"role": "user", "content": "hi"}], None)


@pytest.mark.parametrize("reply,names,content", [
    ('<tool_call>{"name": "stock_quote", "arguments": {"symbols": ["TCS"]}}</tool_call>', ["stock_quote"], ""),
    ('<tool_call>{"name":"stock_quote","arguments":{"symbols":["TCS"]}}', ["stock_quote"], ""),          # no closing tag
    ('Let me look.\n<tool_call>{"name": "browser_click", "arguments": {"element_id": 4}}</tool_call>\n'
     '<tool_call>{"name": "stock_quote", "arguments": {"symbols": ["X"]}}</tool_call>', ["browser_click", "stock_quote"], "Let me look."),
    ('```json\n{"name": "stock_quote", "arguments": {"symbols": ["TCS"]}}\n```', ["stock_quote"], ""),
    ("TCS is at **3,000**.", [], "TCS is at **3,000**."),
    ('<tool_call>{"name": "stock_quote", "arguments": {"note": "a } inside"}}</tool_call>', ["stock_quote"], ""),
])
def test_parse_reply(reply, names, content):
    msg = kiro_bridge.parse_reply(reply)
    assert [c["function"]["name"] for c in msg.get("tool_calls", [])] == names
    assert msg["content"] == content
    for c in msg.get("tool_calls", []):
        assert isinstance(json.loads(c["function"]["arguments"]), dict) and c["id"].startswith("call_")


def _calls(text):
    return [(c["function"]["name"], json.loads(c["function"]["arguments"]))
            for c in kiro_bridge.parse_reply(text).get("tool_calls", [])]


def test_parse_reply_repairs_small_json_mistakes():
    # the real failure: a form fill one closing brace short, followed by an upload call
    spot = ('Filling.\n<tool_call>{"name": "browser_fill", "arguments": {"fields": {"12": "Asha", "17": "No"}}</tool_call>\n'
            '<tool_call>{"name": "browser_upload", "arguments": {"element_id": 20, "file_path": "C:\\\\cv.pdf"}}</tool_call>')
    assert _calls(spot) == [("browser_fill", {"fields": {"12": "Asha", "17": "No"}}),
                            ("browser_upload", {"element_id": 20, "file_path": "C:\\cv.pdf"})]
    assert _calls('<tool_call>{"name": "read_file", "arguments": {"path": "C:\\Users\\a.txt"}}</tool_call>') == \
        [("read_file", {"path": "C:\\Users\\a.txt"})]                                     # invalid escapes
    assert _calls('<tool_call>{"name": "x", "arguments": {"a": [1, 2}}</tool_call>') == [("x", {"a": [1, 2]})]
    assert _calls('<tool_call>{"name": "x", "arguments": {"a": 1,}}</tool_call>') == [("x", {"a": 1})]
    assert _calls('<tool_call>{"name": "x", "arguments": "{\\"a\\": 1}"}</tool_call>') == [("x", {"a": 1})]
    assert _calls('<tool_call>{"name": "x", "a": 1}</tool_call>') == [("x", {"a": 1})]   # arguments at top level
    assert _calls('<tool_call>{"name": "x", "arguments": null}</tool_call>') == [("x", {})]


def test_parse_reply_never_drops_a_broken_call():
    # cut off inside a string: never guess the rest (it could be an email body); report it instead
    calls = _calls('<tool_call>{"name": "send_email", "arguments": {"to": "a@b.c", "body": "Hello, I wanted to')
    assert calls[0][0] == "send_email" and "_unparsed" in calls[0][1]
    # unreadable call text, and a call after it: both come back, in order
    calls = _calls('Ok.\n<tool_call>browser_click 4</tool_call>\n<tool_call>{"name": "y", "arguments": {}}</tool_call>')
    assert calls[0][0] == "invalid_tool_call" and "_unparsed" in calls[0][1] and calls[1] == ("y", {})
    # a repaired call at the very end of a reply with no closing tag may be cut off: not trusted
    assert "_unparsed" in _calls('<tool_call>{"name": "x", "arguments": {"id": 4}')[0][1]
    # prose and fenced JSON that isn't a call stay a normal answer
    assert kiro_bridge.parse_reply('Here: ```json\n{"name": "Asha", "city": "Pune"}\n```')["content"].startswith("Here")


def test_parse_arguments_is_lenient_but_safe():
    from karya.registry import parse_arguments
    assert parse_arguments('{"fields": {"1": "a"}') == {"fields": {"1": "a"}}
    assert parse_arguments('{"path": "D:\\work\\cv.pdf"}') == {"path": "D:\\work\\cv.pdf"}
    assert "_unparsed" in parse_arguments('{"text": "cut off')
    assert parse_arguments('{"a": 1} trailing words') == {"a": 1}


class FakeBridge:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def prompt(self, text, model, timeout=240):
        self.prompts.append((text, model))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def kiro_provider():
    return Provider("kiro", "kiro-cli://acp", "ksk_test", "qwen3-coder-next", fallback_models=("glm-5",),
                    max_input_tokens=40_000, context_chars=160_000, timeout=60)


def test_client_routes_kiro_and_parses_tool_calls(monkeypatch):
    bridge = FakeBridge(['<tool_call>{"name": "stock_quote", "arguments": {"symbols": ["TCS.NS"]}}</tool_call>'])
    monkeypatch.setattr(kiro_bridge, "bridge_for", lambda key, lane="main": bridge)
    seen = {}

    def tools_for(p, budget=None):
        seen["budget"] = budget
        return TOOLS

    msg, used = LLMClient([kiro_provider()]).chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "TCS?"}],
                                                  tools_for, system_for=lambda b: f"PROMPT budget={b}")
    assert used.label == "kiro:qwen3-coder-next" and seen["budget"] == 40_000
    assert msg["tool_calls"][0]["function"]["name"] == "stock_quote"
    text, model = bridge.prompts[0]
    assert model == "qwen3-coder-next" and "PROMPT budget=40000" in text and "stock_quote" in text


def test_kiro_errors_fall_back(monkeypatch):
    bridge = FakeBridge([kiro_bridge.KiroError("model", "model 'qwen3-coder-next' is not available"), "All done."])
    monkeypatch.setattr(kiro_bridge, "bridge_for", lambda key, lane="main": bridge)
    client = LLMClient([kiro_provider()])
    msg, used = client.chat([{"role": "user", "content": "hi"}])
    assert used.model == "glm-5" and msg["content"] == "All done."            # next Kiro model
    assert client.cooldown[("kiro", "qwen3-coder-next")] == float("inf")

    bridge2 = FakeBridge([kiro_bridge.KiroError("auth", "API key rejected")])
    monkeypatch.setattr(kiro_bridge, "bridge_for", lambda key, lane="main": bridge2)
    other = Provider("custom", "http://127.0.0.1:9/v1", "k", "m", timeout=2)
    with pytest.raises(LLMError) as err:
        LLMClient([kiro_provider(), other]).chat([{"role": "user", "content": "hi"}])
    assert "API key rejected" in str(err.value) and len(bridge2.prompts) == 1  # not retried on the second Kiro model


def test_kiro_key_in_custom_fields_is_recognised(monkeypatch, tmp_path):
    from karya import config as cfg
    monkeypatch.setattr(cfg, "ENV_FILE", tmp_path / "missing.env")
    for preset in cfg.PRESETS:
        monkeypatch.delenv(preset.key_env, raising=False)
        monkeypatch.delenv(f"{preset.name.upper()}_MODEL", raising=False)
    for k in ("LLM_PROVIDERS", "CUSTOM_BASE_URL", "CUSTOM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    monkeypatch.setenv("CUSTOM_API_KEY", "ksk_test_placeholder_value")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    providers = cfg.Settings().providers
    assert [p.name for p in providers] == ["kiro", "groq"]
    kiro = providers[0]
    assert kiro.model == "qwen3-coder-next" and kiro.fallback_models == ("glm-5",) and "ksk_" not in repr(kiro)
    monkeypatch.setenv("KIRO_MODEL", "glm-5")
    assert cfg.Settings().providers[0].model == "glm-5"
