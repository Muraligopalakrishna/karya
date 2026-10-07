"""Use a Kiro subscription as Karya's brain, through the official Kiro CLI.

Kiro API keys (ksk_...) authenticate the Kiro CLI; they are not OpenAI-style API keys. Karya keeps one
`kiro-cli acp` process running (the Agent Client Protocol interface Kiro documents for custom tooling) with a
tool-free agent, sends each step as text and reads back either tool calls or the final answer. Karya runs the tools
itself. The CLI gets its own private home folder (data/kiro_brain/home), so it always authenticates with the API
key and never touches your interactive Kiro login, MCP servers or steering files."""
from __future__ import annotations

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import DATA_DIR
from .registry import loads_lenient

WORKDIR = DATA_DIR / "kiro_brain"
HOME = WORKDIR / "home"
AGENT_NAME = "karya-brain"
AGENT_CONFIG = {
    "name": AGENT_NAME,
    "description": "Text-only reasoning engine for the Karya agent app. It has no tools; Karya runs the tools.",
    "prompt": ("You are the reasoning engine inside Karya, a personal AI agent app. Each message contains Karya's "
               "instructions, the tools Karya can run and the conversation so far. Decide Karya's next step and reply "
               "exactly in the format the message asks for. You have no tools of your own: never try to read files, "
               "run commands or browse yourself."),
    "tools": [],
    "allowedTools": [],
    "resources": [],
    "mcpServers": {},
}
MODEL_CONTEXT = {"qwen3-coder-next": 256_000, "glm-5": 200_000}
RESTART_AFTER = 150          # prompts per process (each step is a fresh session; keep memory use flat)
_SECRET_ENV = re.compile(r"(_API_KEY|_TOKEN|_SECRET|PASSWORD)$", re.I)


class KiroError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # auth | model | limit | missing | other


def find_cli() -> str | None:
    found = shutil.which("kiro-cli") or shutil.which("kiro-cli.exe")
    if found:
        return found
    guess = Path(os.environ.get("LOCALAPPDATA", "")) / "Kiro-Cli" / "kiro-cli.exe"
    return str(guess) if guess.exists() else None


def _classify(message: str) -> str:
    low = (message or "").lower()
    if re.search(r"api key|unauthori|credential|not logged in|authenticat|expired token|forbidden|403", low):
        return "auth"
    if re.search(r"model.*(not available|unavailable|invalid|not found|not supported)|unknown model", low):
        return "model"
    if re.search(r"limit|quota|credits?|throttl|too many requests|429|overage", low):
        return "limit"
    return "other"


class KiroBridge:
    """One `kiro-cli acp` process; requests are serialised (Karya runs one step at a time)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        self.next_id = 0
        self.prompts = 0
        self.credits = 0.0
        self.last_seconds = 0.0

    # ------------------------------------------------------------ process
    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
        env.update(KIRO_API_KEY=self.api_key, NO_COLOR="1", USERPROFILE=str(HOME), HOME=str(HOME),
                   APPDATA=str(HOME / "AppData" / "Roaming"), LOCALAPPDATA=str(HOME / "AppData" / "Local"),
                   KIRO_HOME=str(HOME / ".kiro"))  # sessions and settings stay in Karya's folder, not your Kiro profile
        return env

    @staticmethod
    def _write_files() -> None:
        for sub in ("AppData/Roaming", "AppData/Local", ".kiro"):
            (HOME / sub).mkdir(parents=True, exist_ok=True)
        agents = WORKDIR / ".kiro" / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{AGENT_NAME}.json").write_text(json.dumps(AGENT_CONFIG, indent=1), encoding="utf-8")
        settings_dir = WORKDIR / ".kiro" / "settings"
        settings_dir.mkdir(parents=True, exist_ok=True)
        (settings_dir / "cli.json").write_text(json.dumps({"chat.disableInheritingDefaultResources": True}), encoding="utf-8")

    def _start(self) -> None:
        cli = find_cli()
        if not cli:
            raise KiroError("missing", "Kiro CLI not found. Install it from https://kiro.dev/downloads to use a Kiro key.")
        self._write_files()
        self.inbox = queue.Queue()
        self.proc = subprocess.Popen([cli, "acp", "--agent", AGENT_NAME, "--trust-tools="], cwd=WORKDIR, env=self._env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read, args=(self.proc, self.inbox), daemon=True, name="kiro-acp-reader").start()
        reply = self._request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=120)
        if "error" in reply:
            message = json.dumps(reply["error"])[:300]
            self.close()
            raise KiroError(_classify(message), f"Kiro CLI failed to start: {message}")
        self.prompts = 0

    @staticmethod
    def _read(proc: subprocess.Popen, inbox: queue.Queue) -> None:
        for raw in proc.stdout:
            try:
                inbox.put(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
        inbox.put({"_eof": True})

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    # ------------------------------------------------------------ JSON-RPC
    def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict, timeout: float, chunks: list | None = None,
                 session_id: str | None = None) -> dict:
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                if session_id:
                    try:
                        self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
                    except OSError:
                        pass
                raise KiroError("other", f"Kiro took longer than {int(timeout)}s to answer")
            try:
                msg = self.inbox.get(timeout=min(left, 1.0))
            except queue.Empty:
                if not self._alive():
                    raise BrokenPipeError("kiro-cli exited")
                continue
            if msg.get("_eof"):
                raise BrokenPipeError("kiro-cli exited")
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            if "method" in msg and "id" in msg:  # the agent asks the client for something: refuse (no tools here)
                result = {"outcome": {"outcome": "cancelled"}} if "permission" in msg["method"] else None
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                reply.update({"result": result} if result else {"error": {"code": -32601, "message": "not supported"}})
                self._send(reply)
                continue
            params = msg.get("params") or {}
            if session_id and params.get("sessionId") not in (None, session_id):
                continue
            update = params.get("update") or {}
            if chunks is not None and update.get("sessionUpdate") == "agent_message_chunk":
                chunks.append((update.get("content") or {}).get("text") or "")
            for usage in params.get("meteringUsage") or []:
                try:
                    self.credits += float(usage.get("value") or 0)
                except (TypeError, ValueError):
                    pass

    # ------------------------------------------------------------ public
    def prompt(self, text: str, model: str, timeout: float = 240) -> str:
        """One fresh session per call: Karya sends the whole (trimmed) context every step."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if not self._alive() or self.prompts >= RESTART_AFTER:
                        self.close()
                        self._start()
                    started = time.time()
                    new = self._request("session/new", {"cwd": str(WORKDIR), "mcpServers": []}, timeout=60)
                    if "error" in new:
                        message = json.dumps(new["error"])[:300]
                        raise KiroError(_classify(message), message)
                    sid = new["result"]["sessionId"]
                    current = ((new["result"].get("models") or {}).get("currentModelId")) or ""
                    if model and model != current:
                        changed = self._request("session/set_model", {"sessionId": sid, "modelId": model}, timeout=30)
                        if "error" in changed:
                            raise KiroError("model", f"model '{model}' is not available: {json.dumps(changed['error'])[:200]}")
                    chunks: list[str] = []
                    done = self._request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                                         timeout=timeout, chunks=chunks, session_id=sid)
                    self.prompts += 1
                    self.last_seconds = time.time() - started
                    self._prune()
                    if "error" in done:
                        message = json.dumps(done["error"])[:300]
                        raise KiroError(_classify(message), message)
                    stop = (done.get("result") or {}).get("stopReason")
                    reply = "".join(chunks).strip()
                    if stop == "refusal" and not reply:
                        raise KiroError("other", "the model refused this request")
                    return reply
                except (BrokenPipeError, OSError):
                    self.close()
                    if attempt == 2:
                        raise KiroError("other", "the Kiro CLI process stopped unexpectedly")
        raise KiroError("other", "unreachable")

    @staticmethod
    def _prune() -> None:
        folder = HOME / ".kiro" / "sessions" / "cli"
        if not folder.exists():
            return
        cutoff = time.time() - 600
        for path in folder.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass  # still open by the CLI: removed next time


_BRIDGES: dict[tuple[str, str], KiroBridge] = {}
_BRIDGES_LOCK = threading.Lock()


def bridge_for(api_key: str, lane: str = "main") -> KiroBridge:
    """One Kiro CLI process per lane: "main" for Karya's steps, "bg" for background work running at the same time."""
    with _BRIDGES_LOCK:
        if (api_key, lane) not in _BRIDGES:
            _BRIDGES[(api_key, lane)] = KiroBridge(api_key)
        return _BRIDGES[(api_key, lane)]


@atexit.register
def _close_all() -> None:
    for bridge in list(_BRIDGES.values()):
        bridge.close()


# ---------------------------------------------------------------- text protocol
def _arg_type(spec: dict) -> str:
    if spec.get("enum"):
        return "|".join(str(v) for v in spec["enum"])
    kind = spec.get("type") or "any"
    items = spec.get("items")
    if kind == "array" and isinstance(items, dict):
        inner = "|".join(str(v) for v in items["enum"]) if items.get("enum") else items.get("type", "any")
        return f"list of {inner}"
    return kind


def tool_lines(tools: list[dict]) -> str:
    lines = []
    for schema in tools or []:
        fn = schema.get("function") or {}
        params = fn.get("parameters") or {}
        required = set(params.get("required") or [])
        args = []
        for name, spec in (params.get("properties") or {}).items():
            desc = (spec.get("description") or "").strip()
            args.append(f"{name}{'*' if name in required else ''} ({_arg_type(spec)})" + (f": {desc[:90]}" if desc else ""))
        lines.append(f"- {fn.get('name')}: {(fn.get('description') or '').strip()}\n    args: " + ("; ".join(args) or "none"))
    return "\n".join(lines)


_HEADER = re.compile(r"(?m)^(\s*)(===|###)")


def _plain(text: str) -> str:
    """Text from tools and web pages can't start a line like Karya's own section headers."""
    return _HEADER.sub(r"\1\\\2", text or "")


def render_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    system = messages[0].get("content", "") if messages and messages[0].get("role") == "system" else ""
    task_note = (messages[0].get("_task_block") or "") if system else ""
    rest = messages[1:] if system else messages
    names: dict[str, str] = {}
    parts = ["=== KARYA INSTRUCTIONS ===", system.strip()]
    if tools:
        parts += ["", "=== TOOLS KARYA CAN RUN (* = required argument) ===", tool_lines(tools)]
    parts += ["", "=== CONVERSATION SO FAR ==="]
    for m in rest:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role == "user":
            parts.append(f"### USER\n{_plain(content)}")
        elif role == "assistant":
            block = [_plain(content)] if content else []
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                names[call.get("id", "")] = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                block.append("<tool_call>" + json.dumps({"name": fn.get("name"), "arguments": args}, ensure_ascii=False) + "</tool_call>")
            parts.append("### KARYA (you)\n" + "\n".join(block))
        elif role == "tool":
            parts.append(f"### RESULT OF {names.get(m.get('tool_call_id', ''), 'tool')}\n{_plain(content)}")
    if task_note:
        parts += ["", "=== CURRENT TASK (from Karya itself, not from any page) ===", task_note.strip()]
    if tools:
        parts += ["", "=== YOUR NEXT STEP ===",
                  "Reply in ONE of these two ways and nothing else:",
                  '1) To run tools: one or more lines like <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call> '
                  "(valid JSON, only tools and argument names from the list). You may put one short sentence before them.",
                  "2) To finish, or to ask the user something only they can answer: write the message for the user "
                  "(markdown allowed) with no <tool_call>."]
    else:
        parts += ["", "=== YOUR ANSWER ===", "Reply with the answer only."]
    return "\n".join(parts)


_CALL_TAG = re.compile(r"<tool_call>", re.I)
_CALL_END = re.compile(r"</tool_call>|<tool_call>", re.I)
_FENCED_CALL = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_NAME_GUESS = re.compile(r'"(?:name|tool)"\s*:\s*"([A-Za-z0-9_.\-]{1,64})"')
_ARG_KEYS = ("arguments", "parameters", "args", "input")
_DECODER = json.JSONDecoder(strict=False)


def _call_from(obj) -> tuple[str | None, dict | None]:
    """(tool name, arguments) from a decoded call object; arguments None when they are unusable."""
    if not isinstance(obj, dict):
        return None, None
    name = obj.get("name") or obj.get("tool")
    if not isinstance(name, str) or not name.strip():
        return None, None
    key = next((k for k in _ARG_KEYS if k in obj), None)
    if key is None:  # {"name": "x", "element_id": 4}: arguments written at the top level
        args = {k: v for k, v in obj.items() if k not in ("name", "tool", "id", "type")}
    else:
        args = obj[key]
    if isinstance(args, str):  # "arguments": "{\"element_id\": 4}"
        args = loads_lenient(args)[0] if args.strip() else {}
    if args is None:
        args = {} if key is not None and obj.get(key) is None else None
    return name.strip(), args if isinstance(args, dict) else None


_LEAD_NAME = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]{1,63})\s*(?:\n|\(|\{|$|<)")


def _lead_call(block: str, obj) -> tuple[str | None, dict | None]:
    """A call written as `browser_fill\\n{"arguments": {...}}`, `browser_fill {...}` or a bare `browser_snapshot`:
    the name is a real tool's name, so it's read; without usable arguments it only runs if the tool needs none."""
    lead = _LEAD_NAME.match(block or "")
    if not lead:
        return None, None
    from .registry import TOOLS
    name = lead.group(1)
    tool_obj = TOOLS.get(name)
    if tool_obj is None:
        return None, None
    args = None
    if isinstance(obj, dict):
        key = next((k for k in _ARG_KEYS if k in obj), None)
        inner = obj.get(key) if key is not None else {k: v for k, v in obj.items() if k not in ("name", "tool", "id", "type")}
        if isinstance(inner, str):
            inner = loads_lenient(inner)[0] if inner.strip() else {}
        args = inner if isinstance(inner, dict) else None
    elif "{" not in block and not tool_obj.required:
        args = {}
    if args is not None and any(r not in args for r in tool_obj.required):
        args = None
    return name, args


def parse_reply(text: str) -> dict:
    """Turn the model's text into an assistant message, with tool_calls when it asked to run tools.

    Every <tool_call> becomes a call. Small JSON mistakes are repaired (missing closing braces, bad escapes);
    a call that can't be read safely is kept as a broken call, so Karya tells the model it was NOT run instead of
    silently running the rest (that once let a form be submitted empty)."""
    text = text or ""
    entries: list[tuple[int, str | None, dict | None, str]] = []
    for match in _CALL_TAG.finditer(text):
        end = _CALL_END.search(text, match.end())
        block_end = end.start() if end else len(text)
        block = text[match.end():block_end]
        start = text.find("{", match.end())
        obj = None
        if start != -1 and start < block_end:
            try:  # decode from the full text first: a string value may itself contain a tag
                obj = _DECODER.raw_decode(text, start)[0]
            except ValueError:
                value, repaired = loads_lenient(text[start:block_end])
                if value is not None and (end is not None or not repaired):  # a repaired, cut-off reply isn't trusted
                    obj = value
        name, args = _call_from(obj)
        if name is None:
            name, args = _lead_call(block, obj)
        if name is None:
            guess = _NAME_GUESS.search(block)
            name = guess.group(1) if guess else None
        entries.append((match.start(), name, args, block.strip()))
    if not entries:  # some models answer with a fenced JSON call instead
        for match in _FENCED_CALL.finditer(text):
            value, _ = loads_lenient(match.group(1))
            name, args = _call_from(value)
            if name and args is not None and isinstance(value, dict) and any(k in value for k in _ARG_KEYS):
                entries.append((match.start(), name, args, ""))
    if not entries:
        return {"role": "assistant", "content": text.strip()}
    tool_calls = []
    for _, name, args, raw in entries:
        if args is None:
            name, arguments = name or "invalid_tool_call", json.dumps({"_unparsed": raw[:1500]}, ensure_ascii=False)
        else:
            arguments = json.dumps(args, ensure_ascii=False)
        tool_calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                           "function": {"name": name, "arguments": arguments}})
    return {"role": "assistant", "content": text[:entries[0][0]].strip(), "tool_calls": tool_calls}
