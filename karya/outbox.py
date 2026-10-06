"""Every email Karya sends, and every address it has really seen.

- Karya never sends the same person a second email unless the user asks for a follow-up.
- Karya only emails addresses that appeared on a page, in a search result, in an email or in the user's messages
  (an address the AI "remembers" may be made up: those bounce or reach strangers).
- Addresses that bounced are never used again.
- One email goes to one business: several businesses in one email would see each other's addresses."""
from __future__ import annotations

import json
import re
import threading
import time

from .config import DATA_DIR, LOG_DIR

OUTBOX_FILE = DATA_DIR / "outbox.json"
SEEN_FILE = DATA_DIR / "cache" / "seen_emails.json"
_LOCK = threading.RLock()
EMAIL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+'-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
_OBFUSCATED = [(re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*", re.I), "@"), (re.compile(r"\s*[\[({<]\s*dot\s*[\])}>]\s*", re.I), ".")]
FREE_MAIL = re.compile(r"(^|\.)(gmail|googlemail|yahoo|ymail|rocketmail|outlook|hotmail|live|msn|icloud|me|mac|aol|"
                       r"proton|protonmail|pm|gmx|zoho|zohomail|yandex|mail|rediffmail|rr|comcast|att|verizon|"
                       r"sbcglobal|bellsouth|cox|charter|earthlink|optonline|btinternet|sky|orange|web|t-online|qq|163|"
                       r"naver)\.[a-z.]{2,10}$", re.I)
BOUNCE = re.compile(r"address not found|wasn'?t delivered|couldn'?t be delivered|could not be delivered|"
                    r"delivery (status notification|has failed|failure)|mailer-daemon|undeliverable|550[ -]5\.1\.1|"
                    r"no such user|user unknown|does not exist|domain .{0,40} (couldn'?t|could not) be found", re.I)
_SYSTEM_SENDERS = re.compile(r"^(mailer-daemon|postmaster|no-?reply|noreply)@", re.I)
_seen_cache: list[str] | None = None


def addresses(text: str) -> list[str]:
    text = str(text or "")
    for pattern, repl in _OBFUSCATED:
        text = pattern.sub(repl, text)
    out = []
    for found in EMAIL_RE.findall(text):
        addr = found.strip(".'").lower()
        if addr not in out and not re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", addr):
            out.append(addr)
    return out


def org(addr: str) -> str:
    """Who an address belongs to: its domain, or the address itself for free mail (gmail, yahoo, rr.com...)."""
    addr = (addr or "").strip().lower()
    domain = addr.rsplit("@", 1)[-1]
    return addr if FREE_MAIL.search(domain) else domain


# ---------------------------------------------------------------- what has really been seen
def _seen() -> list[str]:
    global _seen_cache
    with _LOCK:
        if _seen_cache is None:
            try:
                data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
                _seen_cache = [a for a in data if isinstance(a, str)] if isinstance(data, list) else []
            except (OSError, ValueError):
                _seen_cache = []
        return _seen_cache


def note_seen(text: str) -> None:
    found = addresses(text)
    if not found:
        return
    with _LOCK:
        seen = _seen()
        new = [a for a in found if a not in seen]
        if not new:
            return
        seen.extend(new)
        del seen[:-5000]
        SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SEEN_FILE.write_text(json.dumps(seen), encoding="utf-8")


def is_seen(addr: str) -> bool:
    return (addr or "").strip().lower() in _seen()


# ---------------------------------------------------------------- what was sent / bounced
def _load() -> dict:
    with _LOCK:
        try:
            data = json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("sent", [])
        data.setdefault("bounced", [])
        if not data.get("seeded"):
            data["seeded"] = True
            data["sent"] = _from_action_log() + data["sent"]
            _save(data)
        return data


def _save(data: dict) -> None:
    with _LOCK:
        OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        OUTBOX_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _from_action_log() -> list[dict]:
    """Emails sent before the outbox existed, from Karya's action log."""
    rows = []
    try:
        lines = (LOG_DIR / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        result = str(r.get("result") or "")
        if r.get("tool") != "send_email" or not r.get("approved"):
            continue
        status = "sent" if result.startswith("Email sent") else "unconfirmed" if result.startswith("UNCONFIRMED") else ""
        if not status:
            continue
        args = r.get("args") or {}
        for addr in addresses(" ".join(str(args.get(k) or "") for k in ("to", "cc", "bcc"))):
            rows.append({"to": addr, "subject": str(args.get("subject") or "")[:150], "time": r.get("time", ""),
                         "status": status})
    return rows


def record(recipients: list[str], subject: str, status: str) -> None:
    data = _load()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for addr in recipients:
        data["sent"].append({"to": addr.strip().lower(), "subject": str(subject or "")[:150], "time": now, "status": status})
    del data["sent"][:-5000]
    _save(data)


def last_sent(addr: str) -> dict | None:
    addr = (addr or "").strip().lower()
    for row in reversed(_load()["sent"]):
        if row.get("to") == addr:
            return row
    return None


def sent_since(stamp: str) -> list[dict]:
    return [r for r in _load()["sent"] if r.get("time", "") >= stamp]


def sent_today() -> int:
    today = time.strftime("%Y-%m-%d")
    return sum(1 for r in _load()["sent"] if r.get("time", "").startswith(today))


# ---------------------------------------------------------------- posts (social media)
def _norm_text(text: str) -> str:
    return re.sub(r"\W+", " ", (text or "").lower()).strip()


def record_post(site: str, text: str) -> None:
    data = _load()
    data.setdefault("posts", []).append({"site": (site or "").lower(), "text": _norm_text(text)[:2000],
                                         "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    del data["posts"][:-2000]
    _save(data)


def posts_today() -> int:
    today = time.strftime("%Y-%m-%d")
    return sum(1 for p in _load().get("posts", []) if p.get("time", "").startswith(today))


def same_post(site: str, text: str, days: int = 7) -> dict | None:
    """The same text was already posted on this site recently (posting it again looks like spam)."""
    want = _norm_text(text)
    if len(want) < 20:
        return None
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    for p in reversed(_load().get("posts", [])):
        if p.get("time", "") >= since and p.get("site") == (site or "").lower() and p.get("text") == want:
            return p
    return None


def mark_bounced(addr: str) -> None:
    data = _load()
    addr = (addr or "").strip().lower()
    if addr and addr not in data["bounced"]:
        data["bounced"].append(addr)
        _save(data)


def is_bounced(addr: str) -> bool:
    return (addr or "").strip().lower() in _load()["bounced"]


_BOUNCED_ADDR = re.compile(r"(?:delivered to|deliver to|delivery to|recipient|address|mailbox|account)\W{0,12}<?\s*"
                           r"([A-Za-z0-9][A-Za-z0-9._%+'-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,24})", re.I)


def note_user_text(text: str, own: set[str] | None = None) -> list[str]:
    """Addresses the user writes are real (they said them). A pasted bounce message marks the address it is about
    as bounced (not every address quoted from the original email)."""
    note_seen(text)
    if not BOUNCE.search(text or ""):
        return []
    own = {a.lower() for a in (own or set()) if a}
    found = [m.group(1).strip(".").lower() for m in _BOUNCED_ADDR.finditer(text or "")]
    if not found:
        found = addresses((text or "")[:300])
    bounced = list(dict.fromkeys(a for a in found if a not in own and not _SYSTEM_SENDERS.match(a)))
    for addr in bounced:
        mark_bounced(addr)
    return bounced
