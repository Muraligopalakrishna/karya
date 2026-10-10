"""Tool registry: every capability the agent can use is registered here with a JSON schema
and a risk level that decides whether the user must approve it first."""
from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

SAFE = "safe"          # runs immediately (read-only / reversible)
CONFIRM = "confirm"    # asks the user unless APPROVAL_MODE=auto
CRITICAL = "critical"  # always asks (send, post, submit, pay, delete, deploy)

MAX_RESULT_CHARS = 12_000

Assessor = Callable[[dict], tuple[str, str]]


def P(type_: str, description: str, **extra: Any) -> dict:
    """Shorthand for one JSON-schema property."""
    schema = {"type": type_, "description": description}
    schema.update(extra)
    return schema


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    required: list[str]
    func: Callable[..., Any]
    risk: str | Assessor = SAFE
    group: str = "general"
    summary: Callable[[dict], str] | None = None
    tags: set = field(default_factory=set)
    # Optional check before asking the user / running: returns a message for the AI when the call should NOT happen
    # (e.g. Submit while required fields are still empty). It can only stop a call, never skip an approval.
    precheck: Callable[[dict], str | None] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": self.params, "required": self.required},
            },
        }

    def assess(self, args: dict) -> tuple[str, str]:
        """Return (risk level, human readable description of what will happen)."""
        if callable(self.risk):
            try:
                return self.risk(args)
            except Exception as exc:  # never let an assessor crash the loop; be cautious instead
                return CRITICAL, f"{self.name}: could not assess risk ({exc})"
        text = self.summary(args) if self.summary else default_summary(self.name, args)
        return self.risk, text


TOOLS: dict[str, Tool] = {}


def default_summary(name: str, args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > 120:
            text = text[:117] + "..."
        parts.append(f"{key}={text}")
    return f"{name}(" + ", ".join(parts) + ")"


def tool(name: str, description: str, params: dict | None = None, required: list[str] | None = None,
         risk: str | Assessor = SAFE, group: str = "general", summary: Callable[[dict], str] | None = None,
         precheck: Callable[[dict], str | None] | None = None):
    """Decorator that registers a function as an agent tool."""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[name] = Tool(name, description, params or {}, list(required or []), func, risk, group, summary,
                           precheck=precheck)
        return func
    return decorator


def schemas(names: list[str] | None = None, brief: bool = False) -> list[dict]:
    chosen = [TOOLS[n] for n in names if n in TOOLS] if names else list(TOOLS.values())
    out = [t.schema() for t in chosen]
    if brief:  # small plans: same tools, ~half the tokens
        for s in out:
            fn = s["function"]
            first = re.split(r"(?<=[.!?])\s", fn["description"], maxsplit=1)[0]
            fn["description"] = first[:150]
            for prop in fn["parameters"]["properties"].values():
                if len(prop.get("description", "")) > 60:
                    prop["description"] = prop["description"][:57] + "..."
    return out


# ---- compact tool routing (small local models get only the groups a request needs) ----
import re as _re  # noqa: E402

GROUP_KEYWORDS = {
    "finance": r"stock|share price|shares|nifty|sensex|market|nasdaq|dow|crypto|bitcoin|invest|ticker|trading|mutual fund|"
               r"ipo|dividend|gold|forex|rupee|usd|inr|price of|sentiment|bullish|bearish|traders?|stocktwits|"
               r"tradingview|what (do )?people (think|say)|people saying|buzz|hype",
    "web": r"crawl|scrap(e|ing)|spider|whole (site|website)|every page|all (the )?pages|forum|threads|research|"
           r"deep dive|investigate|find out|look into|what'?s happening|analy[sz]e",
    "jobs": r"job|hiring|apply|application|resume|\bcv\b|freelanc|gig|upwork|fiverr|internship|career|interview|recruit|"
            r"proposal|\bbid|find work|client|salary|funded|funding|raised|startups?\b|workday|openings?|vacanc",
    "email": r"e-?mail|\bmail|inbox|gmail|outlook|send .* to|reply",
    "browser": r"\bpost\b|linkedin|twitter|\bx\b|tweet|instagram|facebook|reddit|whatsapp|threads|log ?in|sign ?in|"
               r"open .*(site|page|\.com|\.in)|click|fill|form|browser|youtube|comment|message|\bdm\b|upload|\.com",
    "pc": r"\bpc\b|computer|laptop|slow|disk|storage|\bram\b|cpu|process|install|command|powershell|terminal|fix|wi-?fi|"
          r"internet|network|battery|\bapp\b|program|driver|clean|temp|startup|screen|volume|clipboard|python|script",
    "files": r"\bfolder|\bfiles?\b(?! ?path)|\bdocuments?\b|\.docx?\b|\bread (the|this|my) |write (a|the|to) |desktop|downloads|"
             r"rename|\bmove\b|\bcopy\b|\bdelete\b|find my",
    "website": r"website|web site|landing page|deploy|vercel|\bhtml\b|\bcss\b|web ?page|build (me )?(a |my )?(portfolio|site)",
    "memory": r"remember|forget|my name|about me|preference",
    "resume": r"resume|\bcv\b|apply|application|job|hiring|internship|cover letter",
    "accounts": r"log ?in|sign ?in|sign ?up|signup|register|account|password|credential|apply|application|linkedin|"
                r"upwork|freelancer|workatastartup|easy apply",
    "agents": r"\bagents?\b|\bbots?\b|teammates?|helpers?|assign|hand (it|this) (over|off)|in the background|(^|\s)@\w|schedul|every (day|morning|evening|night|hour|week|monday|tuesday|wednesday|"
              r"thursday|friday|saturday|sunday|\d+ ?(min|hour|h\b))|daily|weekly|hourly|twice a day|each (day|morning)|"
              r"remind|recurring|in the background|keep (an eye|watching|checking)|watch (for|my)|monitor|alert me|"
              r"whatsapp|from my (phone|mobile)|on my (phone|mobile)",
}
ALL_GROUPS = sorted(set(GROUP_KEYWORDS) | {"web"})
CORE_TOOLS = ["web_search", "fetch_url", "news_search", "find_contacts", "remember", "enable_tools", "ask_user", "task_status"]
# Groups that are always useful together (applying for a job needs the browser, the resume and saved logins).
GROUP_COMPANIONS = {"jobs": {"resume", "browser", "accounts"}, "accounts": {"browser"}, "resume": {"jobs"}}


def route_groups(text: str) -> set[str]:
    low = (text or "").lower()
    groups = {g for g, pattern in GROUP_KEYWORDS.items() if _re.search(pattern, low)}
    try:                                    # "ask Maya to..." names one of the user's bots
        from . import scheduler
        if any(_re.search(rf"(?<!\w){_re.escape(a['name'].lower())}(?!\w)", low) for a in scheduler.load()):
            groups.add("agents")
    except Exception:  # noqa: BLE001 - routing never fails because of the bots file
        pass
    for g in list(groups):
        groups |= GROUP_COMPANIONS.get(g, set())
    return groups


def names_for(groups: set[str] | None, compact: bool) -> list[str]:
    """Tool names to expose. Full mode: everything except the enable_tools helper.
    Compact mode (small plans / local models): only the groups this request needs, and only their essential tools."""
    if not compact:
        return [n for n in TOOLS if n != "enable_tools"]
    wanted = set(groups or set())
    names = []
    for n, t in TOOLS.items():
        if n in CORE_TOOLS:
            names.append(n)
        elif t.group in wanted:
            essential = ESSENTIAL.get(t.group)
            if essential is None or n in essential:
                names.append(n)
    return names


# For small plans: the tools each group really needs (others can be loaded with enable_tools).
ESSENTIAL = {
    "jobs": {"find_jobs", "choose_jobs", "get_job_details", "get_application_profile", "track_application", "search_freelance",
             "application_queue", "find_funded_companies", "set_job_preferences"},
    "browser": {"browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_fill", "browser_select",
                "browser_check", "browser_upload", "browser_scroll", "browser_read_text", "browser_press", "social_compose",
                "browser_click_at", "browser_drag", "browser_move_piece", "how_to_post"},
    "accounts": {"list_accounts", "request_credentials", "vault_new_password", "browser_type_secret"},
    "files": {"read_file", "write_file", "list_files", "find_files"},
    "resume": {"get_resume_data", "import_resume", "tailor_resume", "add_resume_skills", "save_resume_data"},
}


def _coerce(value: Any, schema: dict) -> Any:
    kind = schema.get("type")
    try:
        if kind == "integer" and not isinstance(value, bool):
            return int(float(value)) if isinstance(value, (str, float)) else value
        if kind == "number" and isinstance(value, str):
            return float(value)
        if kind == "boolean" and isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        if kind == "array" and isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [v.strip() for v in text.split(",") if v.strip()]
        if kind == "object" and isinstance(value, str):
            return json.loads(value)
    except (ValueError, json.JSONDecodeError):
        return value
    return value


_DECODER = json.JSONDecoder(strict=False)  # strict=False: raw newlines/tabs inside strings are fine


def _fix_escapes(text: str) -> str:
    """Inside JSON strings, a backslash that doesn't start a valid escape (C:\\Users) becomes a literal backslash."""
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str and ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt and nxt in '"\\/bfnrt':
                out.append(text[i:i + 2])
                i += 2
                continue
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2:i + 6]):
                out.append(text[i:i + 6])
                i += 6
                continue
            out.append("\\\\")
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
        out.append(ch)
        i += 1
    return "".join(out)


def _drop_trailing_comma(out: list[str]) -> None:
    i = len(out) - 1
    while i >= 0 and out[i] in " \t\r\n":
        i -= 1
    if i >= 0 and out[i] == ",":
        del out[i]


def _close_brackets(text: str) -> str | None:
    """Add closing braces/brackets the model forgot (outside strings), drop stray ones and trailing commas.
    Returns None when a string is left open: the text was cut off, and guessing the rest would be unsafe."""
    out: list[str] = []
    stack: list[str] = []
    in_str = esc = False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if ch not in stack:
                continue  # stray closer
            while stack[-1] != ch:  # e.g. {"a": [1} -> {"a": [1]}
                _drop_trailing_comma(out)
                out.append(stack.pop())
            _drop_trailing_comma(out)
            stack.pop()
            out.append(ch)
            if not stack:
                break  # first complete value; ignore anything after it
            continue
        out.append(ch)
    if in_str:
        return None
    while stack:
        _drop_trailing_comma(out)
        out.append(stack.pop())
    return "".join(out)


def loads_lenient(text: str) -> tuple[Any, bool]:
    """Parse the JSON value at the start of model-written text. Returns (value, repaired).
    Repairs: invalid backslash escapes, missing/stray closing braces, trailing commas. (None, False) if hopeless."""
    text = (text or "").strip()
    if not text:
        return None, False
    try:
        return _DECODER.raw_decode(text)[0], False
    except ValueError:
        pass
    fixed = _fix_escapes(text)
    for candidate in (fixed, _close_brackets(fixed)):
        if candidate:
            try:
                return _DECODER.raw_decode(candidate)[0], True
            except ValueError:
                continue
    return None, False


def parse_arguments(raw: Any) -> dict:
    """Tolerant JSON parsing for model-produced tool arguments."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = str(raw).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start != -1:
        value, _ = loads_lenient(text[start:])
        if isinstance(value, dict):
            return value
    return {"_unparsed": text}


def to_text(result: Any, limit: int = MAX_RESULT_CHARS) -> str:
    if result is None:
        text = "Done."
    elif isinstance(result, str):
        text = result
    else:  # compact JSON: same information, ~30% fewer tokens than indented JSON
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > limit:
        text = text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text or "(empty)"


def run_tool(name: str, args: dict) -> str:
    tool_obj = TOOLS.get(name)
    if tool_obj is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(sorted(TOOLS))}"
    if "_unparsed" in args:
        return f"ERROR: arguments were not valid JSON: {args['_unparsed'][:300]}"
    clean = {}
    for key, value in args.items():
        if key in tool_obj.params:
            clean[key] = _coerce(value, tool_obj.params[key])
    missing = [r for r in tool_obj.required if r not in clean or clean[r] in (None, "")]
    if missing:
        return f"ERROR: missing required argument(s) {missing} for {name}."
    try:
        return to_text(tool_obj.func(**clean))
    except Exception as exc:
        tb = traceback.format_exc(limit=3)
        return f"ERROR: {type(exc).__name__}: {exc}\n{tb[-800:]}"
