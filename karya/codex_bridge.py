"""Use a ChatGPT plan (Plus, Pro, Business, Edu...) as Karya's brain, through the official Codex CLI.

Sign in once (Setup > Connect your AI > ChatGPT runs `codex login`, which opens the ChatGPT sign-in page). Each step
then runs `codex exec` with everything that could act switched off: no shell, no web search, no apps, plugins or
browser, a read-only sandbox and approvals set to "never", in an empty folder, without your Codex config and without
saving the session. Karya sends the step as text and reads back the tool calls or the answer (the same text format as
the Kiro bridge) and runs its tools itself. Usage counts against your ChatGPT plan's Codex limits."""
from __future__ import annotations

import glob
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

WORKDIR = DATA_DIR / "codex_brain"
# Codex features that could act, browse or add tools. Only the ones the installed version has are switched off
# (an unknown name makes Codex refuse to start).
ACTING_FEATURES = ("shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external", "computer_use",
                   "in_app_browser", "multi_agent", "multi_agent_v2", "image_generation", "plugins", "hooks", "goals",
                   "tool_suggest", "skill_mcp_dependency_install", "workspace_dependencies", "memories",
                   "shell_snapshot", "js_repl", "code_mode")
DEFAULT_MODEL = "gpt-5.4-mini"
_SECRET_ENV = re.compile(r"(_API_KEY|_TOKEN|_SECRET|PASSWORD)$", re.I)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CACHE: dict = {}
LOGIN: dict = {"state": "idle", "url": "", "code": "", "error": "", "proc": None}


class CodexError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # auth | model | limit | missing | other


def _classify(message: str) -> str:
    low = (message or "").lower()
    if re.search(r"401|unauthori|not logged in|log ?in again|sign in|authenticat|token (expired|invalid)|missing bearer", low):
        return "auth"
    if re.search(r"usage limit|rate limit|429|too many requests|quota|limit reached|try again (in|at)", low):
        return "limit"
    if re.search(r"model.*(not (found|supported|available)|does not exist|unknown)|unsupported model|invalid model", low):
        return "model"
    return "other"


def find_cli() -> list[str] | None:
    """How to run Codex: its native exe when it can be found (fast, no shell quoting), else node + codex.js."""
    override = os.environ.get("CODEX_CLI", "").strip()
    if override and Path(override).exists():
        return [override]
    found = shutil.which("codex")
    bases = []
    if found:
        bases.append(Path(found).resolve().parent / "node_modules" / "@openai" / "codex")
    if os.environ.get("APPDATA"):
        bases.append(Path(os.environ["APPDATA"]) / "npm" / "node_modules" / "@openai" / "codex")
    for base in dict.fromkeys(bases):
        exes = (glob.glob(str(base / "node_modules" / "@openai" / "codex-win32-*" / "vendor" / "*" / "bin" / "codex.exe"))
                + glob.glob(str(base / "vendor" / "*" / "codex" / "codex.exe"))
                + glob.glob(str(base / "vendor" / "*" / "bin" / "codex.exe")))
        if exes:
            return [exes[0]]
        script, node = base / "bin" / "codex.js", shutil.which("node")
        if script.exists() and node:
            return [node, str(script)]
    if found and found.lower().endswith(".exe"):
        return [found]
    return None


def _env() -> dict:
    """Codex signs in with your ChatGPT login (CODEX_HOME), never with API keys from Karya's environment."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    env.update(NO_COLOR="1", RUST_LOG="error")
    return env


def _run(args: list[str], timeout: float = 60, data: bytes | None = None) -> subprocess.CompletedProcess:
    cli = find_cli()
    if not cli:
        raise CodexError("missing", "Codex CLI isn't installed. Install Node.js, then run: npm install -g @openai/codex")
    return subprocess.run(cli + args, input=data, capture_output=True, timeout=timeout, env=_env(),
                          cwd=str(_workdir()), creationflags=_NO_WINDOW)


def _workdir() -> Path:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    return WORKDIR


def _features_off() -> list[str]:
    """--disable flags for the acting features this Codex version has switched on."""
    if "features" not in _CACHE:
        off = []
        try:
            out = _run(["features", "list"], timeout=30).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                m = re.match(r"^(\S+)\s+(.+?)\s+(true|false)\s*$", line.strip())
                if m and m.group(1) in ACTING_FEATURES and m.group(3) == "true" and "removed" not in m.group(2):
                    off.append(m.group(1))
        except (CodexError, OSError, subprocess.SubprocessError):
            off = ["shell_tool"]
        _CACHE["features"] = off
    return [arg for name in _CACHE["features"] for arg in ("--disable", name)]


# ---------------------------------------------------------------- sign-in
def login_status(fresh: bool = False) -> dict:
    """{'installed', 'signed_in', 'method'} - never any part of a key or token."""
    cached = _CACHE.get("status")
    if cached and not fresh and time.time() - cached[0] < 60:
        return cached[1]
    if not find_cli():
        out = {"installed": False, "signed_in": False,
               "install": "Install Node.js (nodejs.org), then run: npm install -g @openai/codex"}
    else:
        try:
            r = _run(["login", "status"], timeout=30)
            text = (r.stdout + r.stderr).decode("utf-8", "replace")
            signed = r.returncode == 0 and "not logged in" not in text.lower()
            method = "ChatGPT" if re.search(r"chatgpt", text, re.I) else ("API key" if signed else "")
            out = {"installed": True, "signed_in": signed, "method": method}
        except (CodexError, OSError, subprocess.SubprocessError) as exc:
            out = {"installed": True, "signed_in": False, "error": str(exc)[:160]}
    _CACHE["status"] = (time.time(), out)
    return out


def start_login(device: bool = False) -> dict:
    """Runs `codex login` in the background: it opens the ChatGPT sign-in page (or, with device=True, shows a code
    to enter at the address it prints). Poll login_progress() for the result."""
    cli = find_cli()
    if not cli:
        return {"state": "error", "error": "Codex CLI isn't installed. Install Node.js, then run: npm install -g @openai/codex"}
    proc = LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        return login_progress()
    LOGIN.update(state="waiting", url="", code="", error="")
    proc = subprocess.Popen(cli + ["login"] + (["--device-auth"] if device else []), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(), cwd=str(_workdir()),
                            creationflags=_NO_WINDOW)
    LOGIN["proc"] = proc

    def watch():
        for raw in proc.stdout:
            line = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", "replace")).strip()
            url = re.search(r"https://\S+", line)
            if url and not LOGIN["url"]:
                LOGIN["url"] = url.group(0).rstrip(".,)")
            code = re.search(r"\b([A-Z0-9]{4,5}-[A-Z0-9]{4,5})\b", line)
            if code:
                LOGIN["code"] = code.group(1)
            if re.search(r"error|failed", line, re.I):
                LOGIN["error"] = line[:200]
        proc.wait()
        _CACHE.pop("status", None)
        LOGIN["state"] = "done" if proc.returncode == 0 and login_status(fresh=True).get("signed_in") else "failed"
    threading.Thread(target=watch, daemon=True, name="codex-login").start()
    time.sleep(2.0)                          # let it print the address
    return login_progress()


def login_progress() -> dict:
    return {k: LOGIN[k] for k in ("state", "url", "code", "error") if LOGIN.get(k)}


def cancel_login() -> None:
    proc = LOGIN.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
    LOGIN.update(state="idle", url="", code="", error="", proc=None)


# ---------------------------------------------------------------- models
def models() -> list[dict]:
    """The models your Codex offers: [{'id', 'name', 'description', 'context'}], best first."""
    cached = _CACHE.get("models")
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    out = []
    try:
        data = json.loads(_run(["debug", "models"], timeout=30).stdout.decode("utf-8", "replace") or "{}")
        for m in data.get("models") or []:
            if m.get("visibility") == "list" and m.get("slug"):
                out.append({"id": m["slug"], "name": m.get("display_name") or m["slug"],
                            "description": (m.get("description") or "")[:120], "context": m.get("context_window")})
    except (CodexError, OSError, ValueError, subprocess.SubprocessError):
        out = []
    _CACHE["models"] = (time.time(), out)
    return out


def context_tokens(model: str) -> int:
    for m in models():
        if m["id"] == model and m.get("context"):
            return int(m["context"])
    return 200_000


# ---------------------------------------------------------------- one step
class CodexBrain:
    def __init__(self):
        self.last_seconds = 0.0
        self.last_usage: dict = {}

    def prompt(self, text: str, model: str, timeout: float = 240, effort: str = "low") -> str:
        cli = find_cli()
        if not cli:
            raise CodexError("missing", "Codex CLI isn't installed (npm install -g @openai/codex).")
        work = _workdir()
        out_file = work / f"last_{threading.get_ident()}.txt"
        out_file.unlink(missing_ok=True)
        args = ["exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                "-C", str(work), "-s", "read-only", "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", f'model_reasoning_effort="{effort}"', *_features_off(), "-o", str(out_file)]
        if model:
            args += ["-m", model]
        started = time.time()
        proc = subprocess.Popen(cli + args + ["-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=_env(), cwd=str(work), creationflags=_NO_WINDOW)
        inbox: queue.Queue = queue.Queue()

        def read():
            for raw in proc.stdout:
                inbox.put(raw)
            inbox.put(None)
        threading.Thread(target=read, daemon=True, name="codex-exec-reader").start()
        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        answer, problem = "", ""
        deadline = started + timeout
        try:
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise CodexError("other", f"Codex took longer than {int(timeout)}s to answer")
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
                kind = event.get("type")
                item = event.get("item") or {}
                if kind == "item.completed" and item.get("type") == "agent_message":
                    answer = item.get("text") or answer
                elif kind == "turn.completed":
                    self.last_usage = event.get("usage") or {}
                elif kind in ("error", "turn.failed"):
                    problem = str(event.get("message") or (event.get("error") or {}).get("message") or "")[:300]
                    if _classify(problem) in ("auth", "model", "limit"):
                        raise CodexError(_classify(problem), problem)
        finally:
            if proc.poll() is None:
                proc.kill()
            self.last_seconds = time.time() - started
        if not answer and out_file.exists():
            answer = out_file.read_text(encoding="utf-8", errors="replace")
        out_file.unlink(missing_ok=True)
        if not answer.strip():
            raise CodexError(_classify(problem), problem or "Codex gave no answer")
        return answer.strip()


BRAIN = CodexBrain()
