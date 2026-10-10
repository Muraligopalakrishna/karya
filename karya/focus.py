"""What the user is asking for right now, and checks that keep the AI on it.

Every user message either starts a NEW task, or belongs to the current one: a go-ahead ("go on", "yes", "send them")
or an amendment ("no prop firms", "don't add the phone number"). The agent keeps the whole current task in front of the
AI with a short log of what's already done, condenses finished tasks, and checks the AI's final answers before they
are shown: not a copy of an older answer, no "sent/submitted" claim without proof, no "shall I...?" when the user asked
Karya to keep going."""
from __future__ import annotations

from contextvars import ContextVar

import json
import re

CURRENT: dict = {}   # the latest task note, for the task_status tool (set by the agent every step)
BLOCK: ContextVar[str] = ContextVar("karya_task_block", default="")   # the same, per run (the chat and each bot)
CRITICAL_OK = "KARYA-OK:"   # prefix of the agent's record of an approved critical action that ran without errors

# ---------------------------------------------------------------- which task a message belongs to
GO_WORDS = {
    "yes", "yeah", "yea", "yep", "yup", "ya", "y", "ok", "okay", "okk", "okey", "k", "kk", "sure", "fine", "done", "good",
    "great", "perfect", "cool", "nice", "correct", "right", "exactly", "approved", "aproved", "approve", "accept", "go",
    "goo", "gooo", "on", "ahead", "continue", "contiue", "countinue", "carry", "keep", "going", "proceed", "next", "nexty",
    "start", "started", "working", "work", "do", "it", "them", "all", "both", "send", "mail", "submit", "apply", "post",
    "resume", "again", "retry", "try", "more", "please", "pls", "plz", "now", "lets", "let's", "let", "us", "for", "that",
    "this", "these", "those", "the", "and", "then", "bro", "bruh", "brother", "guys", "man", "quickly", "quick", "fast",
    "asap", "away", "same", "one", "ones", "rest", "remaining", "others", "other", "mode", "full", "speed", "u", "you",
    "can", "just", "already", "told", "said", "with", "in", "to", "a", "an",
}
KEEP_GOING = re.compile(
    r"\b(don'?t stop|dont stop|do not stop|never stop|no stop|non ?stop|without stopping|keep going|keep working|keep doing|"
    r"keep it going|go on mode|go on|carry on|continue|full mode|until (you|it'?s|its|it is|the|i|we|all|done|finished|"
    r"complete|everything)|untill|untl|till (you|it|the|done)|do it all|do (them )?all|all of them|finish (it|all|"
    r"everything|them))\b", re.I)
NEW_EXPLICIT = re.compile(
    r"\b(new task|another task|different task|next task|forget (it|that|this|about)|leave (it|that|this)|"
    r"stop (this|that|it) and|switch to|now (i want|let'?s|do|find|go|build|make|write|search|check|tell))\b", re.I)
NEW_START = re.compile(
    r"^\W*(now\s+)?(i\s+(want|wanted|need|would like)\s+(you\s+)?to|can you|could you|please|pls|go to|open|find|search|"
    r"look (for|up)|research|build|create|make|design|write|draft|check|analy[sz]e|tell me|show me|give me|list|get me|"
    r"post|apply|sell|buy|update|learn|teach|explain|what|how|who|which|when|where)\b", re.I)
STRONG_REF = re.compile(
    r"\b(above|again|already|same|the list|this list|that list|those|these|you did|you didn'?t|you did not|you sent|"
    r"you switched|you said|you told|what are you|why are you|why did you|you are|you'?re|instead|more|rest|remaining|"
    r"previous|earlier|last one|that one|this one)\b", re.I)
WEAK_START = re.compile(
    r"^\W*(no|not|nope|nah|don'?t|dont|do not|stop|wait|but|also|and|remove|add|change|why|wrong|it|its|it'?s|this|that|"
    r"you|ur|your|see|bro|brother|bruh|ok but|okay but|only|just)\b", re.I)
PASTED = re.compile(r"approval needed|address not found|wasn'?t delivered|delivery (status|subsystem)|mailer-daemon|"
                    r"^\W*\?|^\s*hi there", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def is_go_ahead(text: str) -> bool:
    words = [w.strip("'") for w in _words(text)]
    return 0 < len(words) <= 8 and all(w in GO_WORDS for w in words)


def wants_keep_going(text: str) -> bool:
    return bool(KEEP_GOING.search(text or ""))


def classify(text: str, has_task: bool) -> str:
    """'continue' (a go-ahead), 'amend' (a correction or more detail for the current task) or 'new' (a new task)."""
    t = (text or "").strip()
    if not has_task or not t:
        return "new"
    if is_go_ahead(t):
        return "continue"
    if NEW_EXPLICIT.search(t):
        return "new"
    if PASTED.search(t[:200]):
        return "amend"  # the user pasted an approval card, a bounce message or a text to use
    words = _words(t)
    if NEW_START.match(t) and len(words) >= 3 and not STRONG_REF.search(t):
        return "new"
    if len(words) <= 14:
        return "amend"
    return "amend" if STRONG_REF.search(t) or WEAK_START.match(t) else "new"


def is_boundary(m: dict) -> bool:
    """A user message that starts a new task (older saved messages have no kind: each starts its own)."""
    return m.get("role") == "user" and m.get("_kind") not in ("continue", "amend")


def task_start(history: list[dict]) -> int | None:
    for i in range(len(history) - 1, -1, -1):
        if is_boundary(history[i]):
            return i
    return None


# ---------------------------------------------------------------- what's been done (the work log)
def call_note(call: dict) -> str:
    fn = call.get("function") or {}
    try:
        data = json.loads(fn.get("arguments") or "{}")
    except (ValueError, TypeError):
        data = {}
    short = ", ".join(f"{k}={v}" for k, v in (data.items() if isinstance(data, dict) else [])
                      if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 90 and not k.startswith("_"))
    if isinstance(data, dict) and isinstance(data.get("to"), list):  # emails: who it went to
        short = f"to={','.join(str(x) for x in data['to'][:4])}" + (f", {short}" if short else "")
    return f"{fn.get('name', '?')}({short[:160]})"


def outcome(result: str) -> str:
    text = (result or "").strip()
    found = re.search(r"RESULT: [A-Z ]+(?: - [^\n]{0,80})?", text)
    if found:
        return found.group(0)
    first = text.splitlines()[0] if text else ""
    if re.match(r"(ERROR|NOT |The user DENIED|UNCONFIRMED|Email sent|Saved|Updated|Filled|Typed|Clicked|Uploaded|"
                r"Selected|Set \[|Entered|Pressed|Karya)", first):
        return first[:110]
    if text[:1] in "[{":
        return "ok (data)"
    if first.startswith("URL:"):
        return "ok: " + first[5:85]
    return "ok"


def work_log(messages: list[dict], limit: int = 25) -> list[str]:
    calls: dict[str, dict] = {}
    lines: list[str] = []
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                calls[c.get("id", "")] = c
        elif m.get("role") == "tool":
            call = calls.get(m.get("tool_call_id", ""))
            if call and (call.get("function") or {}).get("name") not in ("task_status",):
                lines.append(f"{call_note(call)} -> {outcome(m.get('content') or '')}")
    return lines[-limit:]


# ---------------------------------------------------------------- the task note the AI sees
def _quote(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def task_info(history: list[dict]) -> dict:
    start = task_start(history)
    if start is None:
        return {"start": len(history), "users": [], "keep_going": False}
    users = [m for m in history[start:] if m.get("role") == "user"]
    return {"start": start, "users": users,
            "keep_going": any(m.get("_keep_going") for m in users)}


def task_tail(info: dict, keep_going: bool) -> str:
    """For the system prompt (changes only when the user writes, so it stays cache-friendly)."""
    users = info.get("users") or []
    if not users:
        return ""
    lines = ["", "CURRENT TASK (work on this; earlier requests in the chat are finished - don't go back to them "
                 "unless the user asks):", f"- The user asked: \"{_quote(users[0].get('content'))}\""]
    later = users[1:][-6:]
    for n, m in enumerate(later):
        tag = "LATEST - do this now; it overrides earlier instructions when they conflict" if n == len(later) - 1 else "then"
        lines.append(f"- {tag}: \"{_quote(m.get('content'))}\"")
    if keep_going:
        lines.append("- KEEP GOING: the user wants this finished without stopping. Don't ask \"shall I...?\" or offer "
                     "options: pick the best option yourself and continue. Finish what works on one site before trying "
                     "another. Stop only for something only the user can do (an OTP, a CAPTCHA, a payment, their "
                     "personal details) and say exactly what you need.")
    return "\n".join(lines)


def task_block(info: dict, history: list[dict], keep_going: bool) -> str:
    """The full note: the task plus what's already done (for the Kiro prompt and the task_status tool)."""
    tail = task_tail(info, keep_going).strip()
    if not tail:
        return ""
    done = work_log(history[info["start"]:])
    if done:
        tail += "\nDone so far in this task (latest last; don't repeat what worked, don't retry what failed the same way):\n"
        tail += "\n".join(f"- {line}" for line in done)
    return tail


# ---------------------------------------------------------------- checks on the AI's final answer
def _set(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) > 2}


def repeated_answer(text: str, earlier: list[str]) -> str | None:
    """The answer is (almost) a copy of a reply to an earlier request: the AI went back to an old task."""
    mine = _set(text)
    if len(mine) < 12:
        return None
    for other in reversed(earlier):
        theirs = _set(other)
        if len(theirs) >= 12 and len(mine & theirs) / len(mine | theirs) >= 0.8:
            return _quote(other, 120)
    return None


_CLAIMS = {
    "email": (re.compile(r"\b(sent|emailed|mailed)\b", re.I), ("Email sent to",),
              re.compile(r"e-?mails?|\bmails?\b|outreach|@|\bmessages?\b|proposals?|pitch|inbox|\bwrote\b|contacted", re.I)),
    "bid": (re.compile(r"\bbids?\b[^.\n]{0,40}\b(placed|submitted|sent)\b|\b(placed|submitted)\b[^.\n]{0,25}\bbids?\b", re.I),
            ("RESULT: SUBMITTED",), re.compile(r"\bbids?\b", re.I)),
    "post": (re.compile(r"\b(posted|published)\b", re.I), ("RESULT: SUBMITTED", "Posted", CRITICAL_OK),
             re.compile(r"\bpost|publish|linkedin|tweet|instagram|facebook|reddit|thread|reel|story|\bit\b", re.I)),
    "submit": (re.compile(r"\b(submitted|applied)\b", re.I), ("RESULT: SUBMITTED", "Saved application", "Updated application"),
               re.compile(r"applic|\bform\b|\bjobs?\b|\broles?\b|position|resume|\bcv\b|\bto\b", re.I)),
}
_NOT_A_CLAIM = re.compile(
    r"(\bnot\b|n't\b|\bnever\b|\bno\b|\bnone\b|\bnothing\b|\bunable\b|\bfailed\b|\bcould ?not\b|\bwill\b|\bto be\b|"
    r"\bready to\b|\bcan\b|\bbe\b|\bonce\b|\bif\b|\buntil\b|\bbefore\b|\bshall\b|\bshould\b|\bwould\b|\bwant\b|\byou\b|"
    r"\byou'?ve\b|\bwhich\b|\balready\b|\bpreviously\b|\bearlier\b|\byesterday\b|\bwere\b[^.\n]{0,20}\bby you\b)\W+(\S+\W+){0,3}$", re.I)
_PAST_SCOPE = re.compile(r"\b(already|earlier|previously|before|today|yesterday|this morning)\b", re.I)
_MAILBOX = re.compile(r"^\s*(folder|items?|box|tab|mail\b|mails\b|section|label)", re.I)


def _is_claim(text: str, found: re.Match, context: re.Pattern) -> bool:
    before = (text or "")[max(0, found.start() - 45):found.start()]
    if _NOT_A_CLAIM.search(before) or _MAILBOX.match((text or "")[found.end():found.end() + 12]):
        return False  # "not sent yet", "the Sent folder"
    return bool(context.search(_sentence(text, found)))


def _sentence(text: str, found: re.Match) -> str:
    return re.split(r"(?<=[.!?\n])", (text or "")[:found.start()])[-1] + (text or "")[found.start():found.end() + 80]


def unverified_claim(text: str, run_results: list[str], task_results: list[str]) -> str | None:
    """The answer says something was sent/submitted/posted, but no tool result proves it.
    Present claims need proof from this run (since the user's last message); 'already/earlier' ones from this task."""
    for kind, (pattern, proof, context) in _CLAIMS.items():
        for found in pattern.finditer(text or ""):
            if not _is_claim(text, found, context):
                continue  # e.g. "I sent you the list" isn't an email claim
            pool = task_results if _PAST_SCOPE.search(_sentence(text, found)) else run_results
            if not any(r.startswith(proof) or any(p in r[:400] for p in proof) for r in pool):
                return kind
    return None


def claims_email(text: str) -> bool:
    pattern, _, context = _CLAIMS["email"]
    return any(_is_claim(text, found, context) for found in pattern.finditer(text or ""))


_ASKS = re.compile(r"(shall i|should i|would you like|do you want|want me to|let me know (if|what|which|whether|once)|"
                   r"which (one|option|platform|site)|or would you prefer|please (confirm|choose|pick|tell me|let me know)|"
                   r"reply with|tell me (if|which|what)|your call|up to you)", re.I)
_BLOCKER = re.compile(r"\b(otp|captcha|verification code|verify|log ?in|sign ?in|password|2fa|two[- ]factor|pay|payment|"
                      r"card|upi|bank|subscribe|membership|upgrade|purchase|your (date of birth|birthday|age|address|pan|"
                      r"aadhaar|id)|approve|approval|denied|deny|permission|on your phone)\b", re.I)


def asks_instead_of_doing(text: str) -> bool:
    """A 'shall I...?' / 'which option?' ending that isn't about something only the user can do."""
    return bool(_ASKS.search((text or "")[-700:])) and not _BLOCKER.search(text or "")


NUDGE_REPEAT = ("KARYA CHECK: your answer repeats your reply to an earlier request (\"{old}\") instead of answering what the "
                "user just said. The user's latest message is: \"{latest}\". Act on that now (don't repeat yourself).")
NUDGE_CLAIM = ("KARYA CHECK: your answer says something was {what}, but no tool result since the user's last message "
               "confirms it (no \"{proof}\"). Don't claim it. Either do it now with the tool, or tell the user plainly "
               "that it was NOT done and why.")
NUDGE_NOT_SENT = ("KARYA CHECK: your answer says these were emailed, but Karya never sent anything to them: {addrs}. "
                  "Don't claim it. Send them now (one send_email per business), or tell the user plainly they were NOT "
                  "sent.")
NUDGE_KEEP_GOING = ("KARYA: the user asked you to keep going until this is done, so don't ask them to choose. Pick the "
                    "best option yourself and continue now. Only stop for something only they can do (an OTP, a "
                    "CAPTCHA, a payment, their personal details) - then say exactly what you need.")
CLAIM_WORDS = {"email": ("sent", "Email sent to"), "submit": ("submitted", "RESULT: SUBMITTED"),
               "bid": ("placed as a bid", "RESULT: SUBMITTED"), "post": ("posted", "RESULT: SUBMITTED")}
