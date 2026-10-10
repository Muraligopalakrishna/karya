"""Karya has one browser and one job pick list (the "desk"). Bots work in parallel with the chat and with each other
for everything else (research, markets, email, files, websites...), but only one run at a time may use the desk, so
two runs never fill forms or post in the same tab.

- A bot keeps the desk from its first browser step until its run ends.
- The user comes first: a bot waits while the user's own chat (or an AI app over MCP) used the browser in the last
  MAIN_GRACE seconds, so it never disturbs what they're doing.
- The chat waits for a bot that has the desk (up to MAIN_WAIT), then says who has it, so the user can stop that bot.
"""
from __future__ import annotations

import asyncio
import time

from .config import settings
from .runctx import Run

DESK_GROUPS = {"browser", "accounts", "jobs"}
DESK_TOOLS = {"crawl_site", "market_sentiment"}     # read social sites in the browser
MAIN_GRACE = 600.0         # seconds after the chat's last browser step during which bots leave the browser alone
MAIN_WAIT = 300.0          # how long the chat waits for a bot to finish with the browser
BOT_WAIT = 1800.0          # how long a bot waits for the browser before it reports that it couldn't use it


def needs_desk(name: str, group: str, args: dict | None = None) -> bool:
    if group in DESK_GROUPS or name in DESK_TOOLS:
        return True
    return name == "send_email" and not settings.email_ready   # then it sends through Gmail in the browser


class Desk:
    def __init__(self) -> None:
        self.holder: Run | None = None
        self.since = 0.0
        self.doing = ""
        self.main_used = 0.0

    def status(self) -> dict:
        if self.holder is None:
            return {"free": True}
        return {"free": False, "bot": self.holder.bot, "agent_id": self.holder.agent_id, "doing": self.doing,
                "since": int(time.time() - self.since)}

    def _why(self, run: Run) -> str:
        if run.is_bot and self.holder is None:
            return ("NOT RUN: the user is using Karya's browser in the chat right now, so you left it alone. Do what "
                    "you can without the browser, and say in your report what's left for later.")
        who = self.holder.bot if self.holder else "another task"
        doing = f' for "{self.doing[:120]}"' if self.doing else ""
        if run.is_bot:
            return (f"NOT RUN: Karya's browser is busy (the bot {who} is using it{doing}). Do what you can without the "
                    "browser, and say in your report what's left for later.")
        return (f"NOT RUN: Karya's browser is busy: the user's bot {who} is using it{doing}. Tell the user they can "
                f"wait for {who} to finish, or stop {who} (say \"stop {who}\"), then try again.")

    async def acquire(self, run: Run, emit=None, wait: float | None = None, doing: str = "") -> str | None:
        """None when this run may use the desk now; otherwise, after waiting, the reason it can't."""
        limit = (MAIN_WAIT if not run.is_bot else BOT_WAIT) if wait is None else wait
        start, told = time.time(), -1.0
        while True:
            now = time.time()
            if not run.is_bot:
                if self.holder is None:
                    self.main_used = now
                    return None
            elif self.holder is not None and self.holder.id == run.id:
                return None
            elif self.holder is None and now - self.main_used >= MAIN_GRACE:
                self.holder, self.since, self.doing = run, now, doing
                return None
            waited = now - start
            if waited >= limit:
                return self._why(run)
            if emit is not None and (told < 0 or waited - told >= 30):
                told = waited
                if self.holder is not None:
                    text = f"Waiting for the browser: {self.holder.bot or 'another task'} is using it..."
                else:
                    text = "Waiting for the browser: you're using it in the chat. I'll carry on when you're done..."
                try:
                    await emit({"type": "status", "text": text})
                except Exception:  # noqa: BLE001 - a closed page never stops the wait
                    pass
            await asyncio.sleep(1.0)

    def release(self, run_id: str) -> None:
        if self.holder is not None and self.holder.id == run_id:
            self.holder, self.doing = None, ""


DESK = Desk()
