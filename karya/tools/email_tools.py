"""Email over SMTP/IMAP (Gmail App Password by default)."""
from __future__ import annotations

import email
import imaplib
import mimetypes
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, make_msgid, parsedate_to_datetime

from ..config import settings
from ..registry import CRITICAL, P, tool
from .pc import resolve

NOT_READY = ("ERROR: email is not set up. Add the email address and an App Password in Karya's Setup (EMAIL_ADDRESS "
             "and EMAIL_APP_PASSWORD in the .env file in the Karya folder) "
             "(Gmail: turn on 2-Step Verification, then create an App Password at "
             "https://myaccount.google.com/apppasswords) and restart Karya. "
             "Or call request_credentials with site='email' so the user can enter it securely. "
             "Alternative: send it through Gmail in the browser with the browser_* tools.")


def _ensure_email() -> bool:
    """Email login comes from .env (Settings) or, if not set there, from the vault entry 'email'."""
    if settings.email_ready:
        return True
    try:
        from .. import vault
        creds = vault.get_secret("email")
    except Exception:
        creds = None
    if creds and creds[0] and creds[1]:
        settings.email_address, settings.email_password = creds[0], creds[1].replace(" ", "")
        return True
    return False


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[;,]", value)
    return [v.strip() for v in value if v and v.strip()]


def _send_summary(args: dict) -> tuple[str, str]:
    to = ", ".join(_as_list(args.get("to")))
    cc = ", ".join(_as_list(args.get("cc")))
    atts = ", ".join(_as_list(args.get("attachments")))
    body = str(args.get("body", ""))
    text = f"Send email to {to}" + (f" (cc {cc})" if cc else "") + f"\nSubject: {args.get('subject', '')}\n\n{body[:700]}"
    if len(body) > 700:
        text += "..."
    if atts:
        text += f"\n\nAttachments: {atts}"
    from .. import outbox
    unseen = [a for a in _as_list(args.get("to")) + _as_list(args.get("cc")) + _as_list(args.get("bcc"))
              if not outbox.is_seen(a) and not outbox.last_sent(a) and a.lower() not in _own_addresses()]
    if unseen:
        text += ("\n\nWARNING: Karya never saw " + ", ".join(unseen) + " on a website or in an email. Only approve if "
                 "you know it's the right address.")
    return CRITICAL, text


def build_message(to, subject, body, cc=None, bcc=None, attachments=None, html=False, in_reply_to=None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.email_address
    msg["To"] = ", ".join(_as_list(to))
    if _as_list(cc):
        msg["Cc"] = ", ".join(_as_list(cc))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if html:
        msg.set_content(re.sub(r"<[^>]+>", "", body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for item in _as_list(attachments):
        path = resolve(item)
        if not path.exists():
            raise FileNotFoundError(f"attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


_AGAIN = re.compile(r"\b(again|resend|re-send|send it again|one more time|follow[- ]?up|remind)\b", re.I)


def _own_addresses() -> set[str]:
    own = {settings.email_address.lower()} if settings.email_address else set()
    try:
        from ..memory import memory_store
        mail = memory_store.load().get("profile", {}).get("email")
        if mail:
            own.add(str(mail).lower())
    except Exception:  # noqa: BLE001
        pass
    return own


def send_precheck(args: dict) -> str | None:
    """Stops an email before the approval card when it would go to a made-up, bounced or already-emailed address, or
    to several businesses at once."""
    from .. import answers, outbox
    everyone = [a.lower() for a in _as_list(args.get("to")) + _as_list(args.get("cc")) + _as_list(args.get("bcc"))]
    own = _own_addresses()
    others = [a for a in everyone if a not in own]
    if not others:
        return None
    orgs = {outbox.org(a) for a in others}
    if len(orgs) > 1:
        return ("NOT SENT: this one email goes to several businesses at once, so each would see the others' addresses. "
                "Send a separate send_email to each business (one call per business).")
    limit = settings.max_emails_per_day
    if limit and outbox.sent_today() >= limit:
        return (f"NOT SENT: today's limit of {limit} emails is reached (more looks like spam and can get the Gmail "
                "account blocked). Continue tomorrow, or the user can raise the limit in Karya's Setup.")
    via_app = args.get("_via") == "mcp"  # an AI app: the user may have given the address in that app's chat
    asked_again = bool(args.get("send_again")) and (
        via_app or bool(_AGAIN.search(answers.RECENT_USER[-1] if answers.RECENT_USER else "")))
    problems = []
    for addr in others:
        prev = outbox.last_sent(addr)
        if outbox.is_bounced(addr):
            problems.append(f"{addr} bounced before (the address doesn't exist)")
        elif prev and not asked_again:
            how = "was probably already sent (unconfirmed)" if prev.get("status") == "unconfirmed" else "already got your email"
            problems.append(f"{addr} {how} on {prev.get('time', '')[:16]} (subject \"{prev.get('subject', '')[:60]}\")")
        elif not prev and not outbox.is_seen(addr) and not via_app:
            problems.append(f"{addr} wasn't found on any page, search result, email or in the user's messages, so it may "
                            "be made up")
    if not problems:
        return None
    return ("NOT SENT: " + "; ".join(problems) + ". Use only real addresses (find_contacts on the business's own website, "
            "or its contact page), leave out businesses without one, and never email the same person twice unless the "
            "user asks for a follow-up (then send_again=true).")


@tool("send_email", "Send an email (optionally with attachments such as the resume). The user must approve it. One email "
      "per business; only addresses you found on a page or that the user gave.", {
    "to": P("array", "Recipient email addresses (one business)", items={"type": "string"}),
    "subject": P("string", "Subject line"),
    "body": P("string", "Email body (plain text unless html=true)"),
    "cc": P("array", "CC addresses", items={"type": "string"}),
    "bcc": P("array", "BCC addresses", items={"type": "string"}),
    "attachments": P("array", "File paths to attach", items={"type": "string"}),
    "html": P("boolean", "Body is HTML"),
    "in_reply_to": P("string", "Message-ID being replied to (from get_email)"),
    "send_again": P("boolean", "Only when the user asked to email the same person again (a follow-up)"),
}, required=["to", "subject", "body"], risk=_send_summary, precheck=send_precheck, group="email")
def send_email(to, subject: str, body: str, cc=None, bcc=None, attachments=None, html: bool = False, in_reply_to: str | None = None,
               send_again: bool = False):
    result = _send_email(to, subject, body, cc, bcc, attachments, html, in_reply_to)
    from .. import outbox
    recipients = _as_list(to) + _as_list(cc) + _as_list(bcc)
    if result.startswith("Email sent"):
        outbox.record(recipients, subject, "sent")
    elif result.startswith("UNCONFIRMED"):
        outbox.record(recipients, subject, "unconfirmed")
    return result


def _send_email(to, subject: str, body: str, cc=None, bcc=None, attachments=None, html: bool = False, in_reply_to: str | None = None):
    if not _ensure_email():
        if _as_list(attachments):
            return NOT_READY + " (Attachments need the email setup; Gmail in the browser can't attach files for Karya.)"
        return _send_with_gmail_web(_as_list(to), subject, body, _as_list(cc), _as_list(bcc), html)
    msg = build_message(to, subject, body, cc, bcc, attachments, html, in_reply_to)
    recipients = _as_list(to) + _as_list(cc) + _as_list(bcc)
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=30) as smtp:
            smtp.login(settings.email_address, settings.email_password)
            smtp.send_message(msg, to_addrs=recipients)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(settings.email_address, settings.email_password)
            smtp.send_message(msg, to_addrs=recipients)
    return f"Email sent to {', '.join(recipients)} (subject: {subject})."


_SEND_LABEL = re.compile(r"^\W*send\b", re.I)
_SENT = re.compile(r"\bmessage sent\b|\bsending\b", re.I)


def _send_with_gmail_web(to: list[str], subject: str, body: str, cc: list[str], bcc: list[str], html: bool = False) -> str:
    """No email password set up: write the email in Gmail (in the browser Karya uses, where the user is logged in)
    and press Send. The user already approved this exact email in the approval card."""
    from urllib.parse import quote, urlencode

    from ..memory import memory_store
    from . import browser as b
    if html:
        from .web import html_to_text
        body = html_to_text(body)[1]
    params = {"view": "cm", "fs": "1", "tf": "1", "to": ",".join(to), "su": subject, "body": body}
    if cc:
        params["cc"] = ",".join(cc)
    if bcc:
        params["bcc"] = ",".join(bcc)
    account = settings.email_address or memory_store.load().get("profile", {}).get("email") or ""
    if account:
        params["authuser"] = account  # the user's own Gmail account, even if others are signed in too
    try:
        opened = b._run("open", "https://mail.google.com/mail/?" + urlencode(params, quote_via=quote), False)
        if isinstance(opened, str) and opened.startswith(("ERROR", "NOT DONE")):
            return f"{NOT_READY} (Gmail in the browser didn't work either: {opened[:200]})"
        backend = b._current()
        send = None
        for _ in range(8):  # Gmail needs a few seconds to open the compose window
            url = backend.url or ""
            if "accounts.google.com" in url:
                return ("ERROR: Gmail isn't logged in in this browser, so nothing was sent. Ask the user to log in to "
                        "Gmail in the Karya tab, or add a Gmail App Password in Setup.")
            if "mail.google.com" in url:
                send = next((it for it in backend.items.values() if _SEND_LABEL.match(it.get("label") or "")
                             and (it.get("role") == "button" or it.get("tag") == "button")), None)
                if send:
                    break
            b._run("wait", 1.5)
        if not send or "mail.google.com" not in (backend.url or ""):
            return "ERROR: couldn't open Gmail's compose window with a Send button, so nothing was sent."
        clicked = b._run("click", send["id"], None, False)
        if isinstance(clicked, str) and clicked.startswith(("ERROR", "NOT DONE")):
            return f"ERROR: couldn't press Gmail's Send button ({clicked[:200]}), so nothing was sent."
        for _ in range(8):
            text = backend.call(backend.all_text)
            if _SENT.search(text or ""):
                return f"Email sent to {', '.join(to + cc + bcc)} through Gmail in the browser (subject: {subject})."
            time.sleep(1)
        return ("UNCONFIRMED: Send was pressed in Gmail but no 'Message sent' appeared. Check the Gmail Sent folder "
                "before sending again (don't send twice).")
    except Exception as exc:  # noqa: BLE001
        return f"{NOT_READY} (Gmail in the browser didn't work either: {str(exc)[:200]})"


def _imap() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(settings.imap_host, timeout=30)
    conn.login(settings.email_address, settings.email_password)
    return conn


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_subtype() == "html":
        from .web import html_to_text
        text = html_to_text(text)[1]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _criteria(unread_only, from_, subject, since_days, query) -> list[str]:
    if query and "gmail" in settings.imap_host:
        return ["X-GM-RAW", '"' + query.replace('"', "") + '"']
    parts = []
    if unread_only:
        parts.append("UNSEEN")
    if from_:
        parts += ["FROM", f'"{from_}"']
    if subject:
        parts += ["SUBJECT", f'"{subject}"']
    if since_days:
        parts += ["SINCE", time.strftime("%d-%b-%Y", time.localtime(time.time() - since_days * 86400))]
    if query:
        parts += ["TEXT", f'"{query}"']
    return parts or ["ALL"]


@tool("read_emails", "List recent emails (newest first) with sender, subject, date and a snippet. "
      "Supports Gmail search syntax in query (e.g. 'from:hr@x.com newer_than:7d').", {
    "limit": P("integer", "How many (default 10)"),
    "unread_only": P("boolean", "Only unread"),
    "from_address": P("string", "Only from this sender"),
    "subject": P("string", "Subject contains"),
    "since_days": P("integer", "Only last N days"),
    "query": P("string", "Gmail-style search query"),
    "folder": P("string", "Mailbox (default INBOX; Gmail sent: [Gmail]/Sent Mail)"),
}, group="email")
def read_emails(limit: int = 10, unread_only: bool = False, from_address: str = "", subject: str = "",
                since_days: int | None = None, query: str = "", folder: str = "INBOX"):
    if not _ensure_email():
        return NOT_READY
    conn = _imap()
    try:
        conn.select(f'"{folder}"', readonly=True)
        status, data = conn.uid("search", None, *_criteria(unread_only, from_address, subject, since_days, query))
        uids = data[0].split()[-max(1, min(limit, 50)):][::-1] if status == "OK" and data and data[0] else []
        rows = []
        for uid in uids:
            _, fetched = conn.uid("fetch", uid, "(FLAGS RFC822.SIZE BODY.PEEK[])")
            raw = next((p[1] for p in fetched if isinstance(p, tuple)), b"")
            meta = next((p[0] for p in fetched if isinstance(p, tuple)), b"").decode(errors="replace")
            msg = email.message_from_bytes(raw, policy=default_policy)
            try:
                date = parsedate_to_datetime(msg["Date"]).strftime("%Y-%m-%d %H:%M")
            except (TypeError, ValueError):
                date = msg.get("Date", "")
            rows.append({"uid": uid.decode(), "from": msg.get("From", ""), "subject": msg.get("Subject", ""),
                         "date": date, "unread": "\\Seen" not in meta,
                         "snippet": _body_text(msg)[:250].replace("\n", " ")})
        return rows or "No emails matched."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@tool("get_email", "Read one email in full by uid (from read_emails). Can save its attachments.", {
    "uid": P("string", "Email uid"),
    "folder": P("string", "Mailbox (default INBOX)"),
    "save_attachments": P("boolean", "Save attachments into workspace/email_attachments"),
}, required=["uid"], group="email")
def get_email(uid: str, folder: str = "INBOX", save_attachments: bool = False):
    if not _ensure_email():
        return NOT_READY
    conn = _imap()
    try:
        conn.select(f'"{folder}"', readonly=True)
        _, fetched = conn.uid("fetch", str(uid).encode(), "(BODY.PEEK[])")
        raw = next((p[1] for p in fetched if isinstance(p, tuple)), None)
        if not raw:
            return f"ERROR: no email with uid {uid}"
        msg = email.message_from_bytes(raw, policy=default_policy)
        attachments = []
        for part in msg.iter_attachments():
            name = part.get_filename() or "attachment"
            entry = {"name": name, "size_kb": round(len(part.get_content() if isinstance(part.get_content(), bytes) else b"") / 1024, 1)}
            if save_attachments:
                folder_path = settings.workspace / "email_attachments"
                folder_path.mkdir(parents=True, exist_ok=True)
                target = folder_path / re.sub(r"[^\w.\- ]", "_", name)
                payload = part.get_payload(decode=True) or b""
                target.write_bytes(payload)
                entry["saved_to"] = str(target)
            attachments.append(entry)
        return {"from": msg.get("From"), "to": msg.get("To"), "cc": msg.get("Cc"), "date": msg.get("Date"),
                "subject": msg.get("Subject"), "message_id": msg.get("Message-ID"),
                "reply_to": [a for _, a in getaddresses([msg.get("Reply-To") or msg.get("From") or ""])],
                "body": _body_text(msg)[:10000], "attachments": attachments}
    finally:
        try:
            conn.logout()
        except Exception:
            pass
