"""Setup > Bots > "Keep this PC awake": stops Windows from going to sleep on its own while Karya runs and a bot is
working or has a schedule, so scheduled bots run on time. The screen can still turn off, and closing the lid or
choosing Sleep still puts the PC to sleep. Off by default."""
from __future__ import annotations

import sys
import threading
import time

from .config import settings

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
_STARTED = False


def wanted(hub) -> bool:
    if not settings.keep_awake:
        return False
    from . import scheduler
    return bool(hub.bots) or any(a.get("enabled") and scheduler.scheduled(a) for a in scheduler.load())


def _loop(hub) -> None:
    import ctypes
    kernel = ctypes.windll.kernel32
    while True:
        try:
            on = wanted(hub)
        except Exception:  # noqa: BLE001 - never let this thread die
            on = False
        # The request belongs to this thread, which lives as long as Karya: refresh it every minute.
        kernel.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        time.sleep(60)


def start(hub) -> None:
    global _STARTED
    if _STARTED or sys.platform != "win32":
        return
    _STARTED = True
    threading.Thread(target=_loop, args=(hub,), daemon=True, name="karya-keep-awake").start()
