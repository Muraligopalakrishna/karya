"""Use a Claude plan (Pro, Max, Team) as Karya's brain, through the official Claude Code CLI.

Sign in once (Setup > Connect your AI > Claude runs `claude auth login`, which opens the Claude sign-in page). Each
step then runs `claude -p` in safe mode with every tool switched off: no built-in tools, no MCP servers, no skills,
no hooks or plugins, no saved session, in an empty folder. Karya sends the step as text, reads back the tool calls or
the answer (the same text format as the Kiro bridge) and runs its tools itself. Usage counts against the user's
Claude plan, or whatever Claude Code is set up with on their PC (an API key, AWS Bedrock, Google Vertex)."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA_DIR

WORKDIR = DATA_DIR / "claude_brain"
DEFAULT_MODEL = "sonnet"
SYSTEM = ("You are the reasoning engine inside Karya, a personal AI agent app. Each message contains Karya's "
          "instructions, the tools Karya can run and the conversation so far. Decide Karya's next step and reply exactly "
          "in the format the message asks for. You have no tools of your own: never try to read files, run commands or "
          "browse yourself.")
MODELS = [
    {"id": "sonnet", "name": "Sonnet", "description": "the latest Claude Sonnet: fast and capable (recommended)"},
    {"id": "opus", "name": "Opus", "description": "the latest Claude Opus: the most capable, uses your limits faster"},
    {"id": "haiku", "name": "Haiku", "description": "the latest Claude Haiku: the fastest"},
    {"id": "fable", "name": "Fable", "description": "the latest Claude Fable"},
]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CACHE: dict = {}
LOGIN: dict = {"state": "idle", "url": "", "code": "", "error": "", "proc": None}


class ClaudeError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # auth | model | limit | missing | other


def _classify(message: str, status: int | None = None) -> str:
    low = (message or "").lower()
    if status in (401, 403) or re.search(r"authenticat|not logged in|log ?in|unauthori|invalid api key|oauth|"
                                         r"credit balance", low):
        return "auth"
    if status == 429 or re.search(r"rate limit|usage limit|limit reached|too many requests|quota", low):
        return "limit"
    if re.search(r"model.*(not (found|available|exist)|issue with the selected)|invalid model|unknown model", low):
        return "model"
    return "other"


def find_cli() -> list[str] | None:
    override = os.environ.get("CLAUDE_CLI", "").strip()
    if override and Path(override).exists():
        return [override]
    found = shutil.which("claude") or shutil.which("claude.exe")
    if found:
        return [found]
    guess = Path(os.environ.get("USERPROFILE") or Path.home()) / ".local" / "bin" / "claude.exe"
    return [str(guess)] if guess.exists() else None


def _env() -> dict:
    """Claude Code signs in its own way (its saved login, AWS or Google credentials): it gets the environment Windows
    started Karya with, never Karya's .env, so a key saved in Karya can't silently switch it to paid API billing or
    change its model."""
    from .config import ORIGINAL_ENV
    env = dict(ORIGINAL_ENV)
    env.update(NO_COLOR="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    return env


def _help() -> str:
    if "help" not in _CACHE:
        try:
            r = _run(["--help"], timeout=30)
            _CACHE["help"] = (r.stdout + r.stderr).decode("utf-8", "replace")
        except (ClaudeError, OSError, subprocess.SubprocessError):
            _CACHE["help"] = ""
    return _CACHE["help"]


def text_only_flags() -> list[str]:
    """Print mode with nothing that could act: no tools, no MCP, no skills, no saved session. Only flags this Claude
    Code version has; without a way to switch its tools off, Karya won't use it."""
    text = _help()
    has = lambda flag: re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is not None  # noqa: E731
    if has("--tools"):
        flags = ["--tools", ""]
    elif has("--disallowedTools"):
        flags = ["--disallowedTools", "Bash Edit Write MultiEdit NotebookEdit Read Glob Grep WebFetch WebSearch Task "
                                      "TodoWrite KillShell BashOutput"]
    else:
        raise ClaudeError("missing", "This Claude Code version can't switch its tools off. Update it: claude update")
    for flag, extra in (("--safe-mode", []), ("--strict-mcp-config", []), ("--disable-slash-commands", []),
                        ("--no-session-persistence", []), ("--permission-mode", ["dontAsk"])):
        if has(flag):
            flags += [flag, *extra]
    return flags


def _workdir() -> Path:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    return WORKDIR


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    cli = find_cli()
    if not cli:
        raise ClaudeError("missing", "Claude Code isn't installed. Get it from https://claude.com/claude-code")
    return subprocess.run(cli + args, capture_output=True, timeout=timeout, env=_env(), cwd=str(_workdir()),
                          creationflags=_NO_WINDOW)


# ---------------------------------------------------------------- sign-in
def _method(data: dict) -> str:
    provider = str(data.get("apiProvider") or "").lower()
    method = str(data.get("authMethod") or "").lower()
    if provider == "bedrock":
        return "AWS Bedrock"
    if provider == "vertex":
        return "Google Vertex"
    if "claude" in method or "oauth" in method or "subscription" in method:
        return "Claude plan"
    if "key" in method or "console" in method:
        return "API key"
    return method or "signed in"


def login_status(fresh: bool = False) -> dict:
    """{'installed', 'signed_in', 'method'}: never any part of a key or token, and no email."""
    cached = _CACHE.get("status")
    if cached and not fresh and time.time() - cached[0] < 60:
        return cached[1]
    if not find_cli():
        out = {"installed": False, "signed_in": False, "install": "https://claude.com/claude-code"}
    else:
        try:
            r = _run(["auth", "status", "--json"], timeout=30)
            data = json.loads(r.stdout.decode("utf-8", "replace").strip() or "{}")
            data = data if isinstance(data, dict) else {}
            signed = bool(data.get("loggedIn"))
            out = {"installed": True, "signed_in": signed, "method": _method(data) if signed else ""}
        except (ClaudeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            out = {"installed": True, "signed_in": False, "error": str(exc)[:160]}
    _CACHE["status"] = (time.time(), out)
    return out


def start_login() -> dict:
    """`claude auth login` in the background: it opens the Claude sign-in page in the user's browser."""
    cli = find_cli()
    if not cli:
        return {"state": "error", "error": "Claude Code isn't installed. Get it from https://claude.com/claude-code"}
    proc = LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        return login_progress()
    LOGIN.update(state="waiting", url="", code="", error="")
    proc = subprocess.Popen(cli + ["auth", "login"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=_env(), cwd=str(_workdir()), creationflags=_NO_WINDOW)
    LOGIN["proc"] = proc

    def watch():
        for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", "replace")).strip()
            url = re.search(r"https://\S+", line)
            if url and not LOGIN["url"]:
                LOGIN["url"] = url.group(0).rstrip(".,)")
            if re.search(r"error|failed", line, re.I):
                LOGIN["error"] = line[:200]
        proc.wait()
        _CACHE.pop("status", None)
        LOGIN["state"] = "done" if proc.returncode == 0 and login_status(fresh=True).get("signed_in") else "failed"
    threading.Thread(target=watch, daemon=True, name="claude-login").start()
    time.sleep(2.0)
    return login_progress()


def login_progress() -> dict:
    return {k: LOGIN[k] for k in ("state", "url", "code", "error") if LOGIN.get(k)}


def cancel_login() -> None:
    proc = LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
    LOGIN.update(state="idle", url="", code="", error="", proc=None)


def models() -> list[dict]:
    """Claude Code takes model aliases (the latest of each family); a full model name can be typed too."""
    return [dict(m) for m in MODELS]


# ---------------------------------------------------------------- one step
class ClaudeBrain:
    def __init__(self):
        self.last_seconds = 0.0
        self.last_usage: dict = {}

    def prompt(self, text: str, model: str, timeout: float = 240, effort: str = "low") -> str:
        cli = find_cli()
        if not cli:
            raise ClaudeError("missing", "Claude Code isn't installed (https://claude.com/claude-code).")
        args = ["-p", "--output-format", "stream-json", "--verbose", *text_only_flags(), "--system-prompt", SYSTEM]
        if model:
            args += ["--model", model]
        if effort and re.search(r"(?<![\w-])--effort(?![\w-])", _help()):
            args += ["--effort", effort]
        started = time.time()
        proc = subprocess.Popen(cli + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=_env(), cwd=str(_workdir()), creationflags=_NO_WINDOW)
        inbox: queue.Queue = queue.Queue()

        def read():
            for raw in proc.stdout:
                inbox.put(raw)
            inbox.put(None)
        threading.Thread(target=read, daemon=True, name="claude-print-reader").start()
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        parts: list[str] = []
        final, problem = None, ""
        deadline = started + timeout
        try:
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise ClaudeError("other", f"Claude took longer than {int(timeout)}s to answer")
                try:
                    raw = inbox.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if raw is None:
                    break
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                kind, sub = event.get("type"), event.get("subtype")
                if kind == "system" and sub == "api_retry":
                    status = event.get("error_status")
                    reason = str(event.get("error") or "")
                    if status in (401, 403) or reason in ("authentication_failed", "invalid_api_key", "billing_error"):
                        # Claude Code would retry for minutes; it won't get better (no login, or no access)
                        raise ClaudeError("auth", f"Claude Code couldn't sign in or use this model (HTTP {status}, "
                                                  f"{reason or 'refused'}). Check its login with: claude auth status")
                    if status == 429 and (event.get("attempt") or 0) >= 3:
                        raise ClaudeError("limit", "Claude's usage limit is reached for now")
                elif kind == "assistant":
                    for block in (event.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text") or "")
                elif kind == "result":
                    self.last_usage = event.get("usage") or {}
                    if event.get("is_error"):
                        problem = str(event.get("result") or event.get("subtype") or "error")[:300]
                        raise ClaudeError(_classify(problem, event.get("api_error_status")), problem)
                    final = str(event.get("result") or "")
        finally:
            if proc.poll() is None:
                proc.kill()
            self.last_seconds = time.time() - started
        answer = (final if final else "".join(parts)).strip()
        if not answer:
            raise ClaudeError(_classify(problem), problem or "Claude gave no answer")
        return answer


BRAIN = ClaudeBrain()
