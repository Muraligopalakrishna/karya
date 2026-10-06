"""Connection to the Karya Browser Link extension running in the user's own Chrome.

The extension connects to Karya's local server (ws://127.0.0.1:<port>/ext) with the install's secret token. Tools run
in worker threads, so `request` hands the message to the server's event loop and waits for the extension's answer."""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import threading
import time

from .config import ROOT

EXTENSION_DIR = ROOT / "karya" / "extension"


class ExtensionUnavailable(RuntimeError):
    pass


def extension_id(manifest_dir=EXTENSION_DIR) -> str | None:
    """Chrome derives an extension's ID from the manifest's public key, so every install has the same ID."""
    try:
        key = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8")).get("key")
        digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    except (OSError, ValueError, TypeError):
        return None
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def disk_version(manifest_dir=EXTENSION_DIR) -> str:
    """The version of the extension files in Karya's folder (the running extension may be older until it reloads)."""
    try:
        return str(json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError):
        return ""


class ExtensionLink:
    def __init__(self) -> None:
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.info: dict = {}
        self.connected_at = 0.0
        self.last_used = 0.0
        self.events: list[str] = []
        self._pending: dict[int, concurrent.futures.Future] = {}
        self._next = 0
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.ws is not None and self.loop is not None and not self.loop.is_closed()

    def status(self) -> dict:
        running, latest = self.info.get("version"), disk_version()
        return {"connected": self.connected, "version": running, "latest": latest,
                "needs_reload": bool(self.connected and latest and running and running != latest),
                "browser": self.info.get("browser"), "since": int(self.connected_at) if self.connected else None}

    # ---------------------------------------------------------------- called by the server (event loop thread)
    def attach(self, ws, loop: asyncio.AbstractEventLoop, info: dict) -> None:
        old = self.ws
        self.ws, self.loop, self.info, self.connected_at = ws, loop, info, time.time()
        if old is not None and old is not ws:
            self._fail_pending("the browser extension reconnected")

    def detach(self, ws) -> None:
        if self.ws is ws:
            self.ws = None
            self._fail_pending("the link to your Chrome closed (Chrome was closed or the extension reloaded)")

    def deliver(self, message: dict) -> None:
        with self._lock:
            future = self._pending.pop(message.get("id"), None)
        if future is not None and not future.done():
            future.set_result(message)

    def event(self, text: str) -> None:
        self.events.append(str(text)[:300])
        del self.events[:-20]

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ExtensionUnavailable(reason))

    # ---------------------------------------------------------------- called from tool threads
    def wait_connected(self, seconds: float) -> bool:
        end = time.time() + seconds
        while not self.connected and time.time() < end:
            time.sleep(0.25)
        return self.connected

    def request(self, method: str, params: dict | None = None, timeout: float = 45.0) -> dict:
        if not self.connected:
            raise ExtensionUnavailable("Karya Browser Link isn't connected. Open Chrome (with the extension "
                                       "installed) and make sure Karya is running.")
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._next += 1
            rid = self._next
            self._pending[rid] = future
        payload = json.dumps({"id": rid, "method": method, "params": params or {}})
        self.last_used = time.time()
        try:
            asyncio.run_coroutine_threadsafe(self.ws.send_text(payload), self.loop).result(timeout=10)
            message = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(f"Your Chrome didn't answer '{method}' within {int(timeout)}s. The page may be busy or "
                               "showing a popup: check the Karya tab.") from None
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if message.get("error"):
            raise RuntimeError(str(message["error"]))
        return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        """Fire-and-forget message (e.g. the 'Karya needs your approval' badge). Safe from any thread."""
        if not self.connected:
            return
        payload = json.dumps({"id": None, "method": method, "params": params or {}})
        try:
            asyncio.run_coroutine_threadsafe(self.ws.send_text(payload), self.loop)
        except RuntimeError:
            pass


link = ExtensionLink()


def write_extension_config(port: int, token: str, folder=EXTENSION_DIR) -> None:
    """The extension reads this file (inside its own folder) to find and authenticate to Karya. Not in git."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
