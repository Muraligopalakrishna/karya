import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request

from karya.config import Provider
from karya.llm import GEMINI_DUMMY_SIGNATURE, LLMClient, LLMError, prepare_messages

RECEIVED = []
COUNTS: dict[str, int] = {}
BUDGETS: list[int] = []


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _completion(message, finish="stop"):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "fake",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.fixture(scope="module")
def fake_server():
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        from fastapi.responses import JSONResponse
        body = await request.json()
        RECEIVED.append(body)
        model = body["model"]
        COUNTS[model] = COUNTS.get(model, 0) + 1
        n = COUNTS[model]
        if model == "missing-model":
            return JSONResponse({"error": {"message": "model not found", "type": "not_found"}}, status_code=404)
        if model == "badkey-model":
            return JSONResponse({"error": {"message": "Invalid API Key", "type": "invalid_request_error",
                                           "code": "invalid_api_key"}}, status_code=401)
        if model == "busy-model":
            return JSONResponse({"error": {"message": "Rate limit reached for model `busy-model` on tokens per minute (TPM): "
                                                      "Limit 8000, Used 8000. Please try again in 30s.", "code": "rate_limit_exceeded"}},
                                status_code=429, headers={"retry-after": "30"})
        if model == "flaky-model" and n == 1:
            return JSONResponse({"error": {"message": "Rate limit reached on requests per minute (RPM)", "code": "rate_limit_exceeded"}},
                                status_code=429, headers={"retry-after": "1"})
        if model == "flaky2-model" and n <= 2:
            return JSONResponse({"error": {"message": "Rate limit reached on tokens per minute (TPM)", "code": "rate_limit_exceeded"}},
                                status_code=429, headers={"retry-after": "1"})
        if model == "daily-model":
            return JSONResponse({"error": {"message": "Rate limit reached on tokens per day (TPD): Please try again in 41m12s",
                                           "code": "rate_limit_exceeded"}}, status_code=429)
        if model == "toolfail-model" and n == 1:
            return JSONResponse({"error": {"message": "Failed to call a function. Please adjust your prompt.",
                                           "type": "invalid_request_error", "code": "tool_use_failed",
                                           "failed_generation": "<function=web_search>{bad"}}, status_code=400)
        if model == "big-model" and n == 1:
            return JSONResponse({"error": {"message": "Request too large for model on tokens per minute (TPM): Limit 8000, Requested 9100",
                                           "type": "tokens", "code": "rate_limit_exceeded"}}, status_code=413)
        if model in ("free-model", "flaky-model", "flaky2-model", "toolfail-model", "big-model"):
            return JSONResponse(_completion({"role": "assistant", "content": f"ok from {model}"}),
                                headers={"x-ratelimit-limit-tokens": "250000"} if model == "free-model" else {})
        last = body["messages"][-1]
        if last["role"] == "tool":
            return _completion({"role": "assistant", "content": "final answer"})
        if "text-toolcall" in str(last.get("content")):
            return _completion({"role": "assistant",
                                "content": '<think>hmm</think>Let me check.<tool_call>{"name": "web_search", "arguments": {"query": "x"}}</tool_call>'})
        return _completion({"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_9", "type": "function",
            "function": {"name": "web_search", "arguments": "{\"query\": \"nifty\"}"},
            "extra_content": {"google": {"thought_signature": "SIG123"}}}]}, "tool_calls")

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=5)


def test_fallback_and_signature_roundtrip(fake_server):
    dead = Provider("groq", "http://127.0.0.1:9/v1", "k", "m", timeout=3)
    gem = Provider("gemini", fake_server, "k", "gemini-test")
    client = LLMClient([dead, gem])
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "nifty?"}]
    msg, used = client.chat(msgs, tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object", "properties": {}}}}])
    assert used.name == "gemini"
    call = msg["tool_calls"][0]
    assert call["function"]["name"] == "web_search"
    assert call["extra_content"]["google"]["thought_signature"] == "SIG123"  # provider extras preserved
    # send it back: Gemini must receive the same signature
    msgs += [msg, {"role": "tool", "tool_call_id": call["id"], "content": "results"}]
    msg2, _ = client.chat(msgs, tools=None)
    assert msg2["content"] == "final answer"
    sent_back = RECEIVED[-1]["messages"][2]["tool_calls"][0]
    assert sent_back["extra_content"]["google"]["thought_signature"] == "SIG123"


def test_model_fallback_within_provider(fake_server):
    p = Provider("custom", fake_server, "k", "missing-model", fallback_models=("good-model",))
    msg, used = LLMClient([p]).chat([{"role": "user", "content": "hi"}])
    assert used.model == "good-model" and msg["tool_calls"]


def test_all_providers_fail():
    client = LLMClient([Provider("ollama", "http://127.0.0.1:9/v1", "k", "m", timeout=2)])
    with pytest.raises(LLMError) as err:
        client.chat([{"role": "user", "content": "hi"}])
    assert "cannot connect" in str(err.value)
    with pytest.raises(LLMError):
        LLMClient([]).chat([{"role": "user", "content": "hi"}])


def test_text_tool_calls_are_parsed(fake_server):
    p = Provider("ollama", fake_server, "k", "local")
    msg, _ = LLMClient([p]).chat([{"role": "user", "content": "text-toolcall please"}])
    assert msg["tool_calls"][0]["function"]["name"] == "web_search"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"query": "x"}
    assert "<think>" not in msg["content"] and "<tool_call>" not in msg["content"]


def test_prepare_messages_per_provider():
    history = [{"role": "assistant", "content": "", "_internal": 1, "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "a", "arguments": {"k": 1}},
         "extra_content": {"google": {"thought_signature": "S"}}}]},
               {"role": "tool", "tool_call_id": "1", "content": ""}]
    groq = prepare_messages(Provider("groq", "", "", "m"), history)
    assert "extra_content" not in groq[0]["tool_calls"][0] and "_internal" not in groq[0]
    assert groq[0]["tool_calls"][0]["function"]["arguments"] == '{"k": 1}'
    assert groq[0]["content"] is None and groq[1]["content"] == "(no output)"
    gem = prepare_messages(Provider("gemini", "", "", "m"), history)
    assert gem[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "S"
    plain = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "a", "arguments": "{}"}}]}]
    gem2 = prepare_messages(Provider("gemini", "", "", "m"), plain)
    assert gem2[0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == GEMINI_DUMMY_SIGNATURE
    assert "extra_content" not in plain[0]["tool_calls"][0]  # original history not mutated



def _recording_trim(messages, max_chars):
    BUDGETS.append(max_chars)
    return messages


def test_rate_limited_model_rotates_and_cools_down(fake_server):
    p = Provider("groq", fake_server, "k", "busy-model", fallback_models=("free-model",))
    client = LLMClient([p])
    msg, used = client.chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok from free-model" and used.label == "groq:free-model"
    assert client.cooldown[("groq", "busy-model")] > time.time() + 20   # retry-after honoured
    before = COUNTS["busy-model"]
    client.chat([{"role": "user", "content": "again"}])
    assert COUNTS["busy-model"] == before                              # not hammered while cooling down
    assert p.model == "busy-model"                                     # provider config itself not mutated


def test_waits_for_cloud_limit_before_slow_offline_model(fake_server):
    notes = []
    groq = Provider("groq", fake_server, "k", "flaky-model")
    offline = Provider("ollama", "http://127.0.0.1:9/v1", "k", "local", slow=True, timeout=2)
    start = time.time()
    msg, used = LLMClient([groq, offline]).chat([{"role": "user", "content": "hi"}], notify=notes.append)
    assert used.name == "groq" and msg["content"] == "ok from flaky-model"
    assert any("waiting" in n for n in notes) and not any("offline" in n for n in notes)
    assert 0.9 < time.time() - start < 10


def test_cancel_interrupts_a_rate_limit_wait(fake_server):
    import threading as th
    cancel = th.Event()
    th.Timer(0.3, cancel.set).start()
    p = Provider("groq", fake_server, "k", "busy-model")   # always 429 with retry-after 30s -> client waits
    start = time.time()
    with pytest.raises(LLMError) as err:
        LLMClient([p]).chat([{"role": "user", "content": "hi"}], cancel=cancel)
    assert "cancelled" in str(err.value)
    assert time.time() - start < 5


def test_tool_use_failed_is_retried_on_same_model(fake_server):
    msg, used = LLMClient([Provider("groq", fake_server, "k", "toolfail-model")]).chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok from toolfail-model" and COUNTS["toolfail-model"] == 2


def test_request_too_large_shrinks_and_retries(fake_server):
    BUDGETS.clear()
    p = Provider("groq", fake_server, "k", "big-model", context_chars=20_000)
    client = LLMClient([p])
    msg, _ = client.chat([{"role": "user", "content": "hi"}], trim=_recording_trim)
    assert msg["content"] == "ok from big-model"
    assert BUDGETS[0] == 20_000 and BUDGETS[1] < BUDGETS[0]
    assert client.limits["groq:big-model"]["max"] == 5_600   # learned from "Limit 8000" in the error (x0.7)
    assert client.budget_tokens(p, "big-model") == 5_600


def test_limits_are_learned_from_headers_and_remembered(fake_server):
    from karya import llm as llm_mod
    p = Provider("groq", fake_server, "k", "free-model", max_input_tokens=6_000)
    client = LLMClient([p])
    assert client.budget_tokens(p, "free-model") == 6_000           # safe start for a plan known to be small
    client.chat([{"role": "user", "content": "hi"}])
    assert client.limits["groq:free-model"]["tpm"] == 250_000         # fake server says: paid plan
    assert client.budget_tokens(p, "free-model") == 175_000           # -> big requests from now on
    again = LLMClient([Provider("groq", fake_server, "k", "free-model", max_input_tokens=6_000)])
    assert again.limits["groq:free-model"]["tpm"] == 250_000          # remembered across restarts
    assert llm_mod.LIMITS_FILE.exists()


def test_auto_model_choice():
    from karya.llm import choose_model
    ids = ["gpt-4.1-mini", "gpt-5-mini", "gpt-5.4-mini", "gpt-5.4-mini-2026-03-01", "gpt-5.5", "text-embedding-3-large",
           "gpt-realtime-mini", "o4-mini", "whisper-1"]
    assert choose_model(ids, (r"^gpt-[\d.]+-mini$", r"^gpt-[\d.]+$")) == "gpt-5.4-mini"
    claude = ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-opus-5-5"]
    assert choose_model(claude, (r"^claude-sonnet", r"^claude-opus")) == "claude-sonnet-4-6"
    assert choose_model(["text-embedding-3-small"], (r".*",)) is None


def test_token_budget_accounts_for_tool_schemas(fake_server):
    from karya.llm import TEXT_CHARS_PER_TOKEN, TOOL_CHARS_PER_TOKEN
    BUDGETS.clear()
    tools = [{"type": "function", "function": {"name": f"t{i}", "description": "x" * 400,
                                               "parameters": {"type": "object", "properties": {}}}} for i in range(10)]
    p = Provider("groq", fake_server, "k", "free-model", context_chars=24_000, max_input_tokens=5_600)
    LLMClient([p]).chat([{"role": "user", "content": "hi"}], tools=tools, trim=_recording_trim)
    tool_tokens = int(len(json.dumps(tools)) / TOOL_CHARS_PER_TOKEN)
    assert BUDGETS[0] == int((5_600 - tool_tokens - 120) * TEXT_CHARS_PER_TOKEN) < 24_000
    sent = RECEIVED[-1]
    assert len(sent["tools"]) == 10 and sent["tool_choice"] == "auto"


def test_any_provider_from_keys(monkeypatch, tmp_path):
    from karya import config as cfg
    monkeypatch.setattr(cfg, "ENV_FILE", tmp_path / "missing.env")      # don't read the developer's real .env
    for preset in cfg.PRESETS:
        monkeypatch.delenv(preset.key_env, raising=False)
        monkeypatch.delenv(f"{preset.name.upper()}_MODEL", raising=False)
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
    monkeypatch.setenv("OLLAMA_ENABLED", "false")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_placeholder")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-custom-choice")
    providers = cfg.Settings().providers
    assert [p.name for p in providers] == ["openai", "anthropic", "groq"]  # paid-quality first, free tiers after
    openai_p, anthropic_p, groq = providers
    assert openai_p.model == "gpt-custom-choice"                         # the user's choice wins
    assert anthropic_p.model == "" and anthropic_p.prefer                # auto-picked from the key's model list
    assert groq.max_input_tokens == 6_000 and not groq.compact_tools     # small start, adapts once limits are known
    assert {"openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"} <= {groq.model, *groq.fallback_models}
    assert "gsk_test_placeholder" not in repr(groq)                      # keys never show up in logs
    monkeypatch.setenv("LLM_PROVIDERS", "groq,anthropic")
    assert [p.name for p in cfg.Settings().providers] == ["groq", "anthropic"]


def test_keeps_waiting_through_several_limit_windows(fake_server):
    notes = []
    groq = Provider("groq", fake_server, "k", "flaky2-model")          # 429 twice, then OK
    offline = Provider("ollama", "http://127.0.0.1:9/v1", "k", "local", slow=True, timeout=2)
    msg, used = LLMClient([groq, offline]).chat([{"role": "user", "content": "hi"}], notify=notes.append)
    assert used.name == "groq" and msg["content"] == "ok from flaky2-model"
    assert sum("waiting" in n for n in notes) == 2 and not any("offline" in n for n in notes)


def test_daily_limit_goes_straight_to_offline_model(fake_server):
    notes = []
    groq = Provider("groq", fake_server, "k", "daily-model")             # "try again in 41m12s"
    offline = Provider("ollama", fake_server, "k", "free-model", slow=True)
    client = LLMClient([groq, offline])
    start = time.time()
    msg, used = client.chat([{"role": "user", "content": "hi"}], notify=notes.append)
    assert used.name == "ollama" and time.time() - start < 5
    assert any("offline" in n for n in notes) and not any("waiting" in n for n in notes)
    assert client.cooldown[("groq", "daily-model")] > time.time() + 2400   # parsed from the message


def test_rejected_key_disables_provider_for_the_session(fake_server):
    groq = Provider("groq", fake_server, "k", "badkey-model", fallback_models=("free-model",))
    client = LLMClient([groq, Provider("custom", fake_server, "k", "free-model")])
    msg, used = client.chat([{"role": "user", "content": "hi"}])
    assert used.name == "custom" and msg["content"] == "ok from free-model"
    assert client.cooldown[("groq", "badkey-model")] == float("inf")
    assert client.cooldown[("groq", "free-model")] == float("inf")   # whole provider skipped, not just one model


def test_odd_unicode_from_gpt_oss_is_normalized():
    import json as js
    from karya.llm import clean_text
    assert clean_text("entry\u2011level, 4\u20116\u202fLPA, 7\u202fdays") == "entry-level, 4-6 LPA, 7 days"
    raw_args = js.dumps({"url": "https://example.com/a\u2011b", "text": "Hello\u202fworld"})          # escaped form
    assert js.loads(clean_text(raw_args)) == {"url": "https://example.com/a-b", "text": "Hello world"}
    literal = '{"path": "C:\\\\u2011dir", "q": "x\u2011y"}'                                           # escaped backslash kept
    assert js.loads(clean_text(literal)) == {"path": "C:\\u2011dir", "q": "x-y"}
    assert clean_text("") == "" and clean_text("normal text - ok") == "normal text - ok"
