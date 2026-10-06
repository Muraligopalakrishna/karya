import asyncio
import time

import pytest

from karya.agent import Agent
from karya.config import settings
from karya.llm import LLMClient

from .conftest import Recorder

pytestmark = [pytest.mark.ollama, pytest.mark.live]


def test_real_local_model_uses_stock_tool():
    ollama = [p for p in settings.providers if p.name == "ollama"]
    assert ollama, "Ollama provider disabled in .env"
    agent = Agent(llm=LLMClient(ollama), persist=False)
    rec = Recorder()
    start = time.time()
    answer = asyncio.run(agent.run("What is the current share price of Reliance Industries (RELIANCE.NS)? "
                                   "Use your stock tool.", rec.emit, rec.confirm))
    elapsed = time.time() - start
    calls = [e["name"] for e in rec.events if e["type"] == "tool_call"]
    print(f"\nlocal model took {elapsed:.0f}s, tools used: {calls}\nanswer: {answer[:400]}")
    assert "stock_quote" in calls or "stock_history" in calls
    assert any(ch.isdigit() for ch in answer)
