"""Login codes (OTP): when a site sends a one-time code, Karya fetches it and types it in itself.

The user, 2026-10-11: Cutshort sent an OTP to their email and Karya just waited for them to type it. Now
enter_login_code finds the code in the site's newest email (Gmail over IMAP when email is set up in Karya, otherwise
the Gmail inbox in Karya's browser, read in a tab of its own) and types it into the page. When there's no such email
(a text message, another inbox), the user is asked once, in the chat and on WhatsApp.

Safety: only a code from an email sent by that same site (its own domain, or the domain it sends mail from), received
in the last few minutes, is used, and only on that site's page. The code isn't saved or logged, and it is hidden from
the AI's view of the page while it is valid."""
from __future__ import annotations

import email
import re
import time
from datetime import datetime
from email.policy import default as default_policy
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote, urlparse

from .registry import P, tool

FRESH_SECONDS = 15 * 60     # a code email older than this is never used
WAIT_SECONDS = 75           # how long Karya waits for the email to arrive
POLL_SECONDS = 6
NEWER_WAIT = 20             # an email older than the request: wait this long for a newer one first

# Sites that send their codes from another domain than their website's.
SENDERS = {
    "instagram": ("instagram.com", "mail.instagram.com", "facebookmail.com"),
    "facebook": ("facebookmail.com", "facebook.com", "meta.com"),
    "threads": ("instagram.com", "threads.net", "facebookmail.com"),
    "x": ("x.com", "twitter.com"),
    "twitter": ("twitter.com", "x.com"),
    "google": ("google.com", "accounts.google.com"),
    "youtube": ("google.com", "youtube.com"),
    "microsoft": ("microsoft.com", "accountprotection.microsoft.com", "live.com"),
    "live": ("microsoft.com", "accountprotection.microsoft.com", "live.com"),
    "outlook": ("microsoft.com", "accountprotection.microsoft.com", "outlook.com"),
    "myworkdayjobs": ("myworkday.com", "myworkdayjobs.com", "workday.com"),
    "myworkday": ("myworkday.com", "myworkdayjobs.com", "workday.com"),
    "amazon": ("amazon.com", "amazon.in", "amazon.jobs"),
    "naukri": ("naukri.com", "naukri.in"),
    "zoho": ("zoho.com", "zoho.in", "zohocorp.com"),
}
_SLD = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "gen", "firm", "ind"}

_KEY = (r"(?:code|otp|one[ -]?time(?: pass(?:word|code)| pin)?|verification|verify|security|confirm(?:ation)?|"
        r"passcode|pin|log ?in|sign ?in|authenticat\w*)")
_PATTERNS = (
    # "123456 is your Instagram code", "G-123456 is your Google verification code"
    re.compile(r"(?<![\w-])(?:[A-Z]-)?(\d{4,8})\b(?=\s+(?:is|as)\s+(?:your|the)\b)", re.I),
    # "Your OTP is 5629", "verification code: 123456", "Your security code\n\n482913"
    re.compile(_KEY + r"\b[^\d]{0,60}?(?<![\d.,:/-])(\d{4,8})(?![\d,]|\.\d|:\d)", re.I),
    # "Use 482913 to verify your account"
    re.compile(r"(?<![\d.,:/-])(\d{4,8})(?![\d,]|\.\d|:\d)[^\d\n]{0,30}?\b(?:to (?:verify|confirm|log ?in|sign ?in|"
               r"continue|complete)|is your)\b", re.I),
    # explicitly labelled letter-and-number codes: "Your code is: AB12CD"
    re.compile(r"(?i:" + _KEY + r")\s*(?i:is\s*:?|:)\s*([A-Z0-9]{5,8})\b"),
)


def site_name(host: str) -> str:
    """'www.instagram.com' -> 'instagram', 'cutshort.io' -> 'cutshort', 'shop.example.co.in' -> 'example'."""
    host = (host or "").lower().split("@")[-1].split(":")[0].strip(". ")
    labels = [p for p in host.split(".") if p]
    if labels and labels[0] == "www":
        labels = labels[1:]
    if len(labels) >= 3 and labels[-2] in _SLD and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")


def _registrable(host: str) -> str:
    host = (host or "").lower().split(":")[0].strip(". ")
    labels = [p for p in host.split(".") if p]
    keep = 3 if len(labels) >= 3 and labels[-2] in _SLD and len(labels[-1]) == 2 else 2
    return ".".join(labels[-keep:])


def sender_domains(host: str) -> tuple[str, ...]:
    own = _registrable(host)
    return tuple(dict.fromkeys(SENDERS.get(site_name(host), ()) + ((own,) if own else ())))


def sender_ok(address: str, host: str) -> bool:
    """The email really comes from this site (its own domain, a subdomain of it, or a known sending domain)."""
    addr = parseaddr(str(address or ""))[1] or str(address or "")
    domain = addr.rpartition("@")[2].lower().strip(">. ")
    if not domain or not host:
        return False
    if any(domain == d or domain.endswith("." + d) for d in sender_domains(host)):
        return True
    return bool(site_name(host)) and site_name(domain) == site_name(host)   # mail.cutshort.co for cutshort.io


def extract_code(*texts: str) -> str | None:
    """The one-time code in an email's subject/body, or None. Years and long numbers are never taken."""
    for text in texts:
        text = re.sub(r"[\u200b-\u200f\u00a0]", " ", str(text or ""))
        for pattern in _PATTERNS:
            for m in pattern.finditer(text):
                code = m.group(1)
                if code.isdigit() and len(code) == 4 and re.fullmatch(r"(19|20)\d\d", code) and \
                        re.search(r"\b(19|20)\d\d\b\s*$", text[:m.end()].strip()[-6:]):
                    continue                        # "© 2026" next to "security"
                if code.isdigit() or re.search(r"\d", code):
                    return code.upper()
    return None


def clean_code(raw) -> str | None:
    """What the user typed as the code ('123 456', '123-456', 'code is 123456') -> '123456'."""
    text = str(raw or "").strip()
    m = re.search(r"(?<![\w])(\d(?:[ -]?\d){3,9})(?![\w])", text)     # digits, maybe in groups: "123 456"
    if m:
        return re.sub(r"[ -]", "", m.group(1))
    for token in re.findall(r"\b[A-Za-z0-9]{4,10}\b", text):           # letters and digits: "K7Q2ZP"
        if re.search(r"\d", token):
            return token.upper()
    return None


# ---------------------------------------------------------------- reading email
def _when(text: str, now: datetime | None = None) -> float | None:
    """Gmail's row time ('Sat, Oct 11, 2026, 1:14 AM', 'Sat, 11 Oct 2026, 01:14') -> epoch seconds."""
    now = now or datetime.now()
    text = str(text or "")
    tm = re.search(r"(\d{1,2}):(\d{2})\s*(am|pm)?", text, re.I)
    months = "jan feb mar apr may jun jul aug sep oct nov dec".split()
    mm = re.search(r"\b(" + "|".join(months) + r")[a-z]*\.?\s+(\d{1,2})\b|\b(\d{1,2})\s+(" + "|".join(months) + r")[a-z]*",
                   text, re.I)
    year = re.search(r"\b(20\d\d)\b", text)
    if not tm or not mm:
        return None
    hour, minute = int(tm.group(1)), int(tm.group(2))
    half = (tm.group(3) or "").lower()
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    month = months.index((mm.group(1) or mm.group(4)).lower()[:3]) + 1
    day = int(mm.group(2) or mm.group(3))
    try:
        return datetime(int(year.group(1)) if year else now.year, month, day, hour, minute).timestamp()
    except ValueError:
        return None


def from_imap(host: str, since: float) -> tuple[str, float] | None:
    """The newest code from this site in the inbox (email set up in Karya: Gmail App Password)."""
    from .config import settings
    if not settings.email_ready:
        return None
    from .tools import email_tools
    conn = email_tools._imap()
    try:
        conn.select('"INBOX"', readonly=True)
        if "gmail" in settings.imap_host:
            query = f"from:({' OR '.join(sender_domains(host))}) newer_than:1d"
            status, data = conn.uid("search", None, "X-GM-RAW", '"' + query + '"')
        else:
            status, data = conn.uid("search", None, "SINCE", time.strftime("%d-%b-%Y", time.localtime(since - 86400)))
        uids = data[0].split()[-15:][::-1] if status == "OK" and data and data[0] else []
        for uid in uids:
            _, fetched = conn.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in fetched if isinstance(p, tuple)), b"")
            msg = email.message_from_bytes(raw, policy=default_policy)
            if not sender_ok(msg.get("From", ""), host):
                continue
            try:
                sent = parsedate_to_datetime(msg["Date"]).timestamp()
            except (TypeError, ValueError):
                continue
            if sent < since:
                continue
            code = extract_code(msg.get("Subject", ""), email_tools._body_text(msg)[:4000])
            if code:
                return code, sent
        return None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


GMAIL_SEARCH = "https://mail.google.com/mail/u/0/#search/"


def from_gmail_web(host: str, since: float) -> tuple[str, float] | None:
    """The newest code from this site in the Gmail inbox open in Karya's browser (read in a tab of its own, which is
    closed again; nothing is clicked)."""
    from .tools import browser
    url = GMAIL_SEARCH + quote(f"from:({' OR '.join(sender_domains(host))}) newer_than:1d", safe="")
    try:
        for attempt in range(3):            # Gmail draws the search results a moment after the page loads
            got = browser._run("crawl_read", url, 0, "gmailRows") or {}
            if isinstance(got, dict) and got.get("signed_out"):
                return None
            rows = (got.get("rows") if isinstance(got, dict) else None) or []
            for index, row in enumerate(rows):
                if not sender_ok(row.get("email") or "", host):
                    continue
                sent = _when(row.get("when") or "")
                if sent is not None and sent < since:
                    return None                  # newest first: nothing recent from this site
                code = extract_code(row.get("subject") or "", row.get("snippet") or "")
                if code:
                    return code, sent or time.time()
                # the code is only inside the email: open it (in Karya's Gmail tab) and read it
                browser._run("crawl_act", "gmailOpen", {"index": index})
                message = {}
                for _ in range(8):
                    time.sleep(1.0)
                    message = browser._run("crawl_act", "gmailMessage", {}) or {}
                    if isinstance(message, dict) and message.get("text"):
                        break
                if isinstance(message, dict) and message.get("text") and \
                        sender_ok(message.get("email") or row.get("email") or "", host):
                    code = extract_code(row.get("subject") or "", message["text"])
                    if code:
                        return code, sent or time.time()
                return None                      # only the newest email from the site counts
            if rows:
                return None
            time.sleep(2.5)
        return None
    finally:
        try:
            browser._run("crawl_close")
        except Exception:  # noqa: BLE001
            pass


def find_code(host: str, since: float) -> tuple[str, float, str] | None:
    """(code, sent_time, where) from the user's email, or None."""
    for reader, where in ((from_imap, "your email"), (from_gmail_web, "your Gmail")):
        try:
            got = reader(host, since)
        except Exception:  # noqa: BLE001 - one way failing (no login, IMAP down) tries the next
            got = None
        if got:
            return got[0], got[1], where
    return None


def wait_for_code(host: str, asked_at: float, wait: float = WAIT_SECONDS, stop=None) -> tuple[str, float, str] | None:
    """Wait (up to `wait` seconds) for the code email: one sent after the site was asked for it, or a recent one
    if no newer email comes within NEWER_WAIT seconds."""
    deadline, older = time.time() + wait, None
    while True:
        got = find_code(host, asked_at - FRESH_SECONDS)
        if got and got[1] >= asked_at - 90:
            return got
        if got:
            older = got
        if older and time.time() - asked_at > NEWER_WAIT:
            return older
        if time.time() >= deadline or (stop is not None and stop.is_set()):
            return older
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- codes in use (hidden from the AI's view)
_ACTIVE: dict[str, float] = {}
NO_CODE = "NO CODE FOUND"


def remember_active(code: str) -> None:
    _ACTIVE[str(code)] = time.time() + FRESH_SECONDS


def active() -> list[str]:
    now = time.time()
    for code in [c for c, until in _ACTIVE.items() if until < now]:
        _ACTIVE.pop(code, None)
    return list(_ACTIVE)


def hide(text: str) -> str:
    """A code in use, wherever it shows up (the field's value, the email page) -> ******."""
    for code in active():
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", "*" * 6, text)
    return text


def page_host() -> str:
    from .tools import browser
    url = browser._run("current_url")
    return urlparse(str(url or "")).netloc.lower() if not str(url).startswith("ERROR") else ""


def type_code(element_id: int, code: str) -> str:
    from .tools import browser
    remember_active(code)
    return browser._run("type_code", int(element_id), str(code))


def _site_title(host: str) -> str:
    name = site_name(host)
    return {"x": "X", "linkedin": "LinkedIn", "myworkdayjobs": "Workday"}.get(name, name.title() or host)


@tool("enter_login_code", "A site sent the user a one-time login code (OTP / verification / security code): Karya "
      "finds it in the user's email (that site's newest email) and types it into the code box. Use it as soon as a "
      "page asks for such a code (click the site's 'send code' / 'get OTP on email' button first if it has one). If "
      "the code isn't in their email (a text message, another inbox), Karya asks the user for it (in the chat and on "
      "WhatsApp). Never ask the user to type a code into the page themselves, and never wait for them to.", {
    "element_id": P("integer", "The code box from the latest snapshot (the first box if there's one per digit)"),
}, required=["element_id"], group="accounts")
def enter_login_code(element_id: int):
    host = page_host()
    if not host:
        return "ERROR: open the page that asks for the code first."
    got = wait_for_code(host, time.time() - 60)
    if not got:
        return (f"{NO_CODE} in the user's email from {_site_title(host)} (looked for {WAIT_SECONDS}s). Karya will ask "
                "the user for it.")
    code, sent, where = got
    page = type_code(element_id, code)
    if page.startswith(("ERROR", "NOT DONE")):
        return page
    return (f"Entered the {len(code)}-character code from {_site_title(host)}'s email in {where} (sent "
            f"{time.strftime('%H:%M', time.localtime(sent))}). Now click the page's Verify / Continue button if it "
            "didn't go on by itself.\n" + page)
