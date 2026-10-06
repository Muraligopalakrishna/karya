import asyncio
import json

import pytest

from karya import registry
from karya.agent import Agent, trim_messages
from karya.llm import LLMError
from karya.registry import CONFIRM, CRITICAL, P, TOOLS, tool

from .conftest import FakeLLM, Recorder, reply, tool_call

RAN = []


@tool("_test_critical", "test tool", {"x": P("string", "x")}, risk=CRITICAL)
def _test_critical(x: str = ""):
    RAN.append(("critical", x))
    return f"critical ran {x}"


@tool("_test_confirm", "test tool", {"x": P("string", "x")}, risk=CONFIRM)
def _test_confirm(x: str = ""):
    RAN.append(("confirm", x))
    return f"confirm ran {x}"


@pytest.fixture(autouse=True)
def clear_ran():
    RAN.clear()
    yield
    RAN.clear()


def run(agent, text, rec):
    return asyncio.run(agent.run(text, rec.emit, rec.confirm))


def test_tool_then_answer():
    llm = FakeLLM([tool_call("remember", {"text": "prefers remote jobs"}), reply("Saved it.")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    assert run(agent, "remember I prefer remote jobs", rec) == "Saved it."
    assert [t for t in rec.types() if t != "status"][-3:] == ["tool_call", "tool_result", "assistant"]
    tool_msg = [m for m in agent.history if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "call_1" and tool_msg["content"].startswith("Remembered")
    # the second LLM call saw the tool result
    assert llm.calls[1]["messages"][-1]["role"] == "tool"
    assert llm.calls[0]["messages"][0]["role"] == "system" and len(llm.calls[0]["tools"]) == len(TOOLS) - 1
    assert rec.requests == []  # safe tool: no approval asked


def test_routing_for_small_local_models():
    assert {"jobs"} <= registry.route_groups("find frontend jobs in Hyderabad and apply")
    assert "finance" in registry.route_groups("what is the share price of TCS")
    assert "browser" in registry.route_groups("post this on LinkedIn")
    assert "pc" in registry.route_groups("my laptop is slow, fix it")
    names = registry.names_for({"finance"}, compact=True)
    assert "stock_quote" in names and "web_search" in names and "enable_tools" in names
    assert "browser_click" not in names and "send_email" not in names
    assert "enable_tools" not in registry.names_for(None, compact=False)

    # a compact (ollama) run starts small, then grows after enable_tools
    llm = FakeLLM([tool_call("enable_tools", {"groups": ["email"]}), reply("ok")], provider_name="ollama")
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    run(agent, "what's the news today?", rec)
    first = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    second = {t["function"]["name"] for t in llm.calls[1]["tools"]}
    assert "send_email" not in first and "send_email" in second and "news_search" in first
    assert "email" in agent.recent_groups


def test_critical_tool_approved():
    llm = FakeLLM([tool_call("_test_critical", {"x": "go"}), reply("done")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[True])
    run(agent, "do it", rec)
    assert RAN == [("critical", "go")]
    assert rec.requests[0]["risk"] == CRITICAL and rec.requests[0]["tool"] == "_test_critical"


def test_critical_tool_denied_is_not_run():
    llm = FakeLLM([tool_call("_test_critical", {"x": "go"}), reply("ok, not doing it")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[False])
    run(agent, "do it", rec)
    assert RAN == []
    assert "DENIED" in [m for m in agent.history if m["role"] == "tool"][0]["content"]
    result = [e for e in rec.events if e["type"] == "tool_result"][0]
    assert result["denied"] is True and result["ok"] is False


def test_auto_mode_skips_confirm_but_not_critical():
    llm = FakeLLM([tool_call("_test_confirm", {"x": "1"}, "c1"), tool_call("_test_critical", {"x": "2"}, "c2"), reply("fin")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[False])
    agent.auto_mode = True
    run(agent, "go", rec)
    assert RAN == [("confirm", "1")]           # confirm-level ran silently
    assert [r["tool"] for r in rec.requests] == ["_test_critical"]  # critical still asked (and denied)


def test_ask_mode_asks_for_confirm_level():
    llm = FakeLLM([tool_call("_test_confirm", {"x": "1"}), reply("fin")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[True])
    agent.auto_mode = False
    run(agent, "go", rec)
    assert RAN == [("confirm", "1")] and len(rec.requests) == 1


def test_unknown_tool_and_bad_args_are_reported_to_model():
    msg = tool_call("does_not_exist", {})
    llm = FakeLLM([msg, reply("sorry")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    run(agent, "x", rec)
    assert "unknown tool" in [m for m in agent.history if m["role"] == "tool"][0]["content"]


def test_broken_call_is_not_run_and_later_calls_are_skipped():
    msg = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "_test_confirm", "arguments": '{"x": "1"}'}},
        {"id": "b", "type": "function", "function": {"name": "_test_critical",
                                                     "arguments": json.dumps({"_unparsed": '{"x": "cut'})}},
        {"id": "c", "type": "function", "function": {"name": "_test_critical", "arguments": '{"x": "3"}'}}]}
    llm = FakeLLM([msg, reply("re-sending")])
    agent, rec = Agent(llm=llm, persist=False), Recorder(answers=[True, True, True])
    agent.auto_mode = True
    run(agent, "go", rec)
    results = {m["tool_call_id"]: m["content"] for m in agent.history if m["role"] == "tool"}
    assert RAN == [("confirm", "1")]                          # only the call before the broken one ran
    assert results["b"].startswith("ERROR: this tool call was not valid JSON") and "NOT run" in results["b"]
    assert results["c"].startswith("NOT RUN") and rec.requests == []   # no approval card for unreadable calls


def test_parallel_calls_each_get_a_result_and_cancel_fills_the_rest():
    msg = {"role": "assistant", "content": "working", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "_test_critical", "arguments": '{"x":"1"}'}},
        {"id": "b", "type": "function", "function": {"name": "_test_critical", "arguments": '{"x":"2"}'}}]}
    llm = FakeLLM([msg, reply("never")])
    agent = Agent(llm=llm, persist=False)

    class CancelOnConfirm(Recorder):
        async def confirm(self, request):
            agent.cancel()
            return False

    rec = CancelOnConfirm()
    text = asyncio.run(agent.run("go", rec.emit, rec.confirm))
    assert text == "Stopped."
    ids = [m["tool_call_id"] for m in agent.history if m["role"] == "tool"]
    assert ids == ["a", "b"]  # every tool call has a matching result, so the next request stays valid
    assert "note" in rec.types()


def test_llm_failure_is_reported():
    llm = FakeLLM([LLMError("no providers")])
    agent, rec = Agent(llm=llm, persist=False), Recorder()
    text = run(agent, "hi", rec)
    assert "couldn't reach" in text and rec.events[-1]["type"] == "error"


def test_history_persists(isolated):
    llm = FakeLLM([reply("hello!")])
    agent, rec = Agent(llm=llm, persist=True), Recorder()
    run(agent, "hi", rec)
    again = Agent(llm=FakeLLM([]), persist=True)
    assert again.visible_history() == [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello!"}]
    again.reset()
    assert Agent(llm=FakeLLM([]), persist=True).history == []


def _history(turns):
    msgs = [{"role": "system", "content": "sys"}]
    for t in range(turns):
        msgs.append({"role": "user", "content": f"question {t}"})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"t{t}", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"x"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"t{t}", "content": "R" * 5000})
        msgs.append({"role": "assistant", "content": f"answer {t}"})
    return msgs


def test_trim_keeps_pairs_and_budget():
    msgs = _history(12)
    out = trim_messages(msgs, 20_000)
    size = sum(len(m.get("content") or "") for m in out)
    assert size <= 20_000
    assert out[0]["role"] == "system" and out[1]["role"] == "user"
    assert out[-1]["content"] == "answer 11"  # newest turn kept
    call_ids = {c["id"] for m in out if m.get("tool_calls") for c in m["tool_calls"]}
    for m in out:
        if m["role"] == "tool":
            assert m["tool_call_id"] in call_ids
    assert trim_messages(msgs[:5], 1_000_000) == msgs[:5]  # under budget: untouched


def test_trim_single_huge_turn():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "Z" * 50_000}]
    out = trim_messages(msgs, 5_000)
    total = sum(len(m.get("content") or "") for m in out)
    assert total <= 5_000 and out[1]["role"] == "user"
    assert out[-1]["content"].endswith("[trimmed]") and len(out[-1]["content"]) > 3_000  # keeps as much as fits


def test_trim_cuts_stale_tool_output_before_the_newest():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    for i in (1, 2):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "a", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": str(i), "content": str(i) * 6_000})
    out = trim_messages(msgs, 9_000)
    old, new = out[3]["content"], out[5]["content"]
    assert len(new) == 6_000 and len(old) < 3_100   # newest result intact, older one shrunk


def test_system_prompt_mentions_profile_and_tools(isolated):
    registry.run_tool("update_profile", {"field": "name", "value": "Asha"})
    prompt = Agent(llm=FakeLLM([]), persist=False).system_prompt()
    assert "Asha" in prompt and "browser_fill" in prompt and "untrusted" in prompt
    assert "request_credentials" in prompt and "find_jobs" in prompt and "tailored" in prompt
