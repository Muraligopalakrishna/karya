"""Terminal version of Karya:  python -m karya.cli"""
from __future__ import annotations

import asyncio
import json

from . import tools  # noqa: F401 - registers tools
from .agent import Agent
from .config import settings


async def emit(event: dict) -> None:
    kind = event.get("type")
    if kind == "tool_call":
        print(f"  -> {event['name']}: {event.get('summary', '')[:160]}")
    elif kind == "tool_result":
        mark = "ok" if event.get("ok") else ("denied" if event.get("denied") else "failed")
        print(f"     [{mark}] {str(event.get('preview', ''))[:200].replace(chr(10), ' ')}")
    elif kind == "note":
        print(f"  ({event['text']})")
    elif kind in ("assistant", "error"):
        print("\nKarya: " + event["text"] + "\n")


async def confirm(request: dict) -> bool:
    print("\n  APPROVAL NEEDED [" + request.get("risk", "") + "]:\n  " + str(request.get("summary", "")).replace("\n", "\n  "))
    answer = await asyncio.to_thread(input, "  Approve? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


async def ask(request: dict) -> dict | None:
    import getpass
    print(f"\n  LOGIN NEEDED for {request.get('site')}: {request.get('reason', '')}")
    print("  (Saved encrypted on this PC; the AI never sees the password. Leave empty to skip.)")
    user = await asyncio.to_thread(input, f"  Username/email [{request.get('username', '')}]: ")
    password = await asyncio.to_thread(getpass.getpass, "  Password: ")
    if not password and not user:
        return None
    return {"username": user.strip() or request.get("username", ""), "password": password}


async def main() -> None:
    agent = Agent()
    print("Karya (terminal). Providers:", ", ".join(p.label for p in settings.providers) or "none")
    print("Commands: /new (clear chat), /auto on|off, /quit\n")
    while True:
        try:
            text = (await asyncio.to_thread(input, "You: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text == "/new":
            agent.reset()
            print("New chat.\n")
            continue
        if text.startswith("/auto"):
            agent.auto_mode = text.endswith("on")
            print("Auto mode:", agent.auto_mode)
            continue
        try:
            await agent.run(text, emit, confirm, ask)
        except KeyboardInterrupt:
            agent.cancel()
    print(json.dumps({"bye": True}))


if __name__ == "__main__":
    asyncio.run(main())
