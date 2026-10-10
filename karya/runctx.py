"""Which run the current step belongs to: the main chat (also WhatsApp tasks and other AI apps over MCP) or one of
the user's bots. Set at the start of each run; asyncio tasks and asyncio.to_thread copy it, so the tools a run calls
and the approvals it asks for know who they're working for."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Run:
    id: str = "main"          # "main", or a bot run's own id
    source: str = "chat"      # chat | phone | schedule | assigned
    bot: str = ""             # the bot's name ("" for the main chat)
    agent_id: str = ""        # the bot's id in data/agents.json

    @property
    def is_bot(self) -> bool:
        return self.id != "main"


RUN: ContextVar[Run] = ContextVar("karya_run", default=Run())


def current() -> Run:
    return RUN.get()
