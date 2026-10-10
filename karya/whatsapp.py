"""Give Karya tasks from your phone: your WhatsApp "Message yourself" chat, read in a WhatsApp Web window of its own.

Only that one chat is used. Karya reads the messages you send yourself there, replies in it (its messages start with
TAG; Karya knows its own messages by their text, so you can start yours with "[Karya]" too) and asks there for
approvals. It never reads or writes any other chat: if another chat is opened in its window,
it does nothing there and goes back to your own chat after a minute. It runs in its own Chrome profile (data/whatsapp_profile), apart from the browser Karya works in,
so tasks never disturb it. Link it once: WhatsApp on your phone > Linked devices > Link a device, and scan the code.
Its window must stay open (it can sit behind other windows; minimizing can pause it)."""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from .config import DATA_DIR, settings

PROFILE = DATA_DIR / "whatsapp_profile"
STATE_FILE = DATA_DIR / "whatsapp_state.json"
TAG = "[Karya]"              # plain text: WhatsApp Web draws emoji as images, which a page read would drop
POLL_SECONDS = 2.5
TITLE = "Karya WhatsApp - keep this window open"
REOPEN_SECONDS = 5

READ_JS = r"""() => {
  const txt = (e) => ((e && (e.innerText || e.textContent)) || '').trim();
  if (document.title !== '__TITLE__') document.title = '__TITLE__';   // so it's clear this window must stay open
  const app = document.querySelector('#pane-side, #side, [data-testid="chat-list"]');
  if (!app) {
    const body = txt(document.body).slice(0, 500);
    const qr = document.querySelector('canvas[aria-label], [data-ref] canvas, div[data-ref]');
    return { state: (qr || /scan|link with phone|log in|log into|use whatsapp on your computer/i.test(body)) ? 'qr' : 'loading' };
  }
  const main = document.querySelector('#main');
  if (!main) return { state: 'app' };
  const head = main.querySelector('header');
  let title = '';
  if (head) {
    const t = head.querySelector('[data-testid="conversation-info-header-chat-title"] span[title], span[title]')
      || head.querySelector('[title]');
    title = t ? (t.getAttribute('title') || txt(t)) : txt(head).split('\n')[0];
  }
  const you = !!(head && head.querySelector('[data-testid="you-label"]')) || /\(you\)/i.test(txt(head));
  // long messages show "Read more": open them so the whole task is read
  [...main.querySelectorAll('[data-testid="caption-read-more-button"], [role="button"]')]
    .filter((b) => /^read more$/i.test(txt(b))).slice(-5).forEach((b) => b.click());
  const out = [], seen = new Set();
  for (const node of main.querySelectorAll('[data-id]')) {
    const id = node.getAttribute('data-id') || '';
    if (!id || seen.has(id)) continue;
    if (node.parentElement && node.parentElement.closest('[data-id]')) continue;   // a quoted message inside one
    if (!node.querySelector('[data-pre-plain-text], .copyable-text, [data-testid="msg-container"]')) continue;
    seen.add(id);
    const pre = node.querySelector('[data-pre-plain-text]');
    // the message's own text: not a quoted message it replies to, and the outermost text element
    const sel = '.selectable-text, [data-testid="selectable-text"]';
    const quoted = (e) => !!e.closest('[data-testid*="quoted"], [aria-label*="uoted"]');
    const own = [...node.querySelectorAll(sel)].filter((e) => !quoted(e) && !(e.parentElement && e.parentElement.closest(sel)));
    const body = own.length ? own[own.length - 1] : pre;
    // old layout: ids start with true_/false_; new layout: plain ids, incoming bubbles have a tail-in
    const incoming = /^false_/.test(id) || !!node.querySelector('.message-in, [data-testid="tail-in"]');
    const stamp = pre ? (((pre.getAttribute('data-pre-plain-text') || '').match(/^\[([^\]]*)\]/) || ['', ''])[1]) : '';
    out.push({ id, mine: !incoming, text: txt(body).slice(0, 4000), stamp });
  }
  const box = !!main.querySelector('footer [contenteditable="true"], [data-testid="conversation-compose-box-input"]');
  return { state: 'chat', title, you, messages: out, box };
}""".replace("__TITLE__", TITLE)


STALE_HOURS = 6     # a task the user sent longer ago than this (Karya was off) isn't run without asking again


def msg_time(stamp: str, now: datetime | None = None) -> float | None:
    """'12:27 am, 11/10/2026' or '18:05, 10/11/2026' (WhatsApp's own format, in the user's locale) -> epoch seconds.
    Day/month order isn't given, so the reading closest to now (and not in the future) wins. None if unreadable."""
    now = now or datetime.now()
    parts = [p.strip() for p in str(stamp or "").split(",") if p.strip()]
    clock_part = next((p for p in parts if ":" in p or "." in p and not re.search(r"\d+[./-]\d+[./-]\d+", p)), "")
    date_part = next((p for p in parts if re.search(r"\d+[./-]\d+[./-]\d+", p)), "")
    tm = re.search(r"(\d{1,2})[:.](\d{2})\s*([ap])?\.?\s*m?\.?", clock_part, re.I)
    dm = re.search(r"(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})", date_part)
    if not tm or not dm:
        return None
    hour, minute = int(tm.group(1)), int(tm.group(2))
    half = (tm.group(3) or "").lower()
    if half == "p" and hour < 12:
        hour += 12
    if half == "a" and hour == 12:
        hour = 0
    a, b, c = (int(x) for x in dm.groups())
    if a > 31:                                      # yyyy-mm-dd
        readings = [(a, b, c)]
    else:
        year = c + 2000 if c < 100 else c
        readings = [(year, b, a), (year, a, b)]     # d/m/y, then m/d/y
    best = None
    for year, month, day in readings:
        try:
            when = datetime(year, month, day, hour, minute)
        except ValueError:
            continue
        if when > now + timedelta(minutes=5):
            continue
        if best is None or abs((now - when).total_seconds()) < abs((now - best).total_seconds()):
            best = when
    return best.timestamp() if best else None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())[:80]


def is_self_chat(info: dict, owner: str) -> bool:
    """The open chat is the user's own "Message yourself" chat: its title says "(You)", or its messages carry the
    user's own number."""
    title = str(info.get("title") or "").strip().lower()
    if info.get("you") or "(you)" in title or title in ("you", "message yourself"):
        return True
    return bool(owner) and any(f"_{owner}@c.us_" in str(m.get("id")) for m in info.get("messages") or [])


def new_commands(messages: list[dict], seen: deque, seen_set: set, sent: deque, primed: bool,
                 stale: list | None = None, now: float | None = None) -> list[str]:
    """The user's new messages to themselves, oldest first. Old ones are never replayed: before the first look, and
    whenever none of the visible messages has been seen before, everything visible is only marked as seen. A new
    message sent more than STALE_HOURS ago (Karya was off) goes to `stale` instead of being run."""
    known = [i for i, m in enumerate(messages) if m.get("id") in seen_set]
    fresh = []
    for i, m in enumerate(messages):
        mid = m.get("id")
        if not mid or mid in seen_set:
            continue
        seen.append(mid)
        seen_set.add(mid)
        if not primed or not known or i < max(known) or not m.get("mine"):
            continue
        text = str(m.get("text") or "").strip()
        if not text or _norm(text) in sent:
            continue                     # media without a caption, or one of Karya's own messages
        when = msg_time(m.get("stamp") or "")
        if when is not None and (now or time.time()) - when > STALE_HOURS * 3600:
            if stale is not None:
                stale.append(text)
            continue
        fresh.append(text)
    while len(seen_set) > 2 * (seen.maxlen or 400):
        seen_set.intersection_update(seen)
    return fresh


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


class WhatsAppBridge:
    """One WhatsApp Web window on its own thread (Playwright's sync API stays on the thread that started it)."""

    def __init__(self, owner: str, on_message):
        self.owner = owner              # the user's number with country code, digits only
        self.on_message = on_message    # called with each new task (from this thread)
        self.state, self.detail = "off", ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._out: queue.Queue = queue.Queue()
        self._sent: deque = deque(maxlen=60)   # Karya's own messages (normalized), kept across restarts
        self._seen: deque = deque(maxlen=400)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.state, self.detail = "starting", "opening WhatsApp Web"
        self._thread = threading.Thread(target=self._run, daemon=True, name="karya-whatsapp")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def send(self, text: str) -> None:
        if str(text or "").strip():
            self._out.put(str(text))

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail, "waiting_to_send": self._out.qsize()}

    # ----- the browser thread
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                while not self._stop.is_set():
                    ctx = self._launch(pw)
                    try:
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=90000)
                        self._loop(page)
                    finally:
                        try:
                            ctx.close()
                        except Exception:  # noqa: BLE001
                            pass
                    if not self._stop.is_set():  # its window was closed: open it again (Disconnect turns it off)
                        self.state = "starting"
                        self.detail = ("Karya's WhatsApp window was closed, so Karya opened it again. Keep it open "
                                       "(it can sit behind other windows).")
                        self._stop.wait(REOPEN_SECONDS)
            self.state, self.detail = "off", ""
        except Exception as exc:  # noqa: BLE001 - reported in Karya's status, never crashes the server
            self.state, self.detail = "error", f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"

    def _launch(self, pw):
        PROFILE.mkdir(parents=True, exist_ok=True)
        options = dict(user_data_dir=str(PROFILE), headless=False, no_viewport=True,
                       ignore_default_args=["--enable-automation"],
                       args=["--disable-blink-features=AutomationControlled", "--window-size=980,860",
                             # keep it working while other windows cover it
                             "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                             "--disable-background-timer-throttling", "--disable-features=CalculateNativeWinOcclusion"])
        errors = []
        for channel in dict.fromkeys([settings.browser_channel, "chrome", "msedge", None]):
            try:
                return pw.chromium.launch_persistent_context(**(dict(options, channel=channel) if channel else options))
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc).splitlines()[0][:80])
        raise RuntimeError("could not start Chrome for WhatsApp: " + " | ".join(errors))

    def _persist(self, welcomed: bool = True) -> None:
        _save_state({"seen": list(self._seen), "welcomed": welcomed, "sent": list(self._sent)})

    def _loop(self, page) -> None:
        """Returns when the window is closed (or Karya stops)."""
        state = _load_state()
        seen: deque = deque(state.get("seen") or [], maxlen=400)
        self._seen = seen
        self._sent.extend(s for s in state.get("sent") or [] if s not in self._sent)
        seen_set = set(seen)
        primed, welcomed, last_nav, unlinked = bool(seen), bool(state.get("welcomed")), 0.0, False
        failures, last_ids = 0, None
        while not self._stop.is_set():
            try:
                if page.is_closed():
                    return
                info = page.evaluate(READ_JS) or {}
                failures = 0
            except Exception:  # noqa: BLE001 - the page is navigating, or the window was closed
                if page.is_closed():
                    return
                failures += 1
                if failures >= 6:        # not just a page change: say so instead of "ready"
                    self.state, self.detail = "starting", "WhatsApp Web isn't answering; Karya is reloading it"
                if failures == 40:       # stuck for ~100 s: load WhatsApp again
                    try:
                        page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded", timeout=60000)
                    except Exception:  # noqa: BLE001
                        pass
                if failures >= 80:       # still stuck: close it and open a new window
                    return
                self._stop.wait(POLL_SECONDS)
                continue
            kind = info.get("state")
            if kind == "qr":
                self.state = "needs_qr"
                self.detail = "Scan the code in the WhatsApp window: WhatsApp on your phone > Linked devices > Link a device"
                unlinked = True
            elif kind == "loading":
                if self.state not in ("ready",):
                    self.state, self.detail = "starting", "WhatsApp Web is loading"
            elif kind == "app" or (kind == "chat" and not is_self_chat(info, self.owner)):
                self.state, self.detail = "opening", "opening your 'Message yourself' chat"
                if self.owner and time.time() - last_nav > 60:
                    last_nav = time.time()
                    try:
                        page.goto(f"https://web.whatsapp.com/send?phone={self.owner}", wait_until="domcontentloaded",
                                  timeout=60000)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                self.state, self.detail = "ready", "send tasks in your 'Message yourself' chat"
                if unlinked:     # just linked: what came up before is already in Karya's chat; don't send it late
                    unlinked, welcomed = False, False
                    while not self._out.empty():
                        self._out.get_nowait()
                messages = info.get("messages") or []
                ids = tuple(m.get("id") for m in messages)
                # WhatsApp draws a chat in steps (the newest message first, then the rest): decide what's new only
                # once the same messages show two looks in a row.
                settled, last_ids = ids == last_ids, ids
                if settled:
                    before = len(seen_set)
                    stale: list[str] = []
                    tasks = new_commands(messages, seen, seen_set, self._sent, primed, stale=stale)
                    primed = True
                    if len(seen_set) != before or not welcomed:
                        self._persist()
                    if not welcomed:
                        welcomed = True
                        self._out.put("Linked. Send me tasks here, e.g. \"find PM jobs in Dubai and apply\". "
                                      "Reply YES or NO when I ask for approval; STATUS shows what I'm doing, STOP stops it.")
                    if stale:
                        quoted = "; ".join(f"\"{s[:80]}\"" for s in stale[:3])
                        self._out.put(f"While I was off you sent {quoted}. I didn't run "
                                      f"{'it' if len(stale) == 1 else 'them'} because {'it is' if len(stale) == 1 else 'they are'} "
                                      f"over {STALE_HOURS} hours old. Send it again if you still want it.")
                    for task in tasks:
                        try:
                            self.on_message(task)
                        except Exception:  # noqa: BLE001
                            pass
                if info.get("box"):
                    self._flush(page)
            self._stop.wait(POLL_SECONDS)

    def _flush(self, page) -> None:
        while not self._out.empty():
            text = self._out.get()
            body = (text if text.startswith(TAG) else f"{TAG} {text}")[:3500]
            try:
                box = page.locator('#main footer [contenteditable="true"], '
                                   '#main [data-testid="conversation-compose-box-input"]').last
                box.click(timeout=5000)
                for n, line in enumerate(body.split("\n")):
                    if n:
                        page.keyboard.press("Shift+Enter")
                    if line:
                        page.keyboard.insert_text(line)
                self._sent.append(_norm(body))
                self._persist()          # before it's on screen: never read back as a task, even after a restart
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001 - try again on the next look
                self._out.put(text)
                return
