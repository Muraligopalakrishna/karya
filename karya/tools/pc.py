"""Control the Windows PC: shell commands, files, apps, processes, health checks."""
from __future__ import annotations

import base64
import datetime as dt
import fnmatch
import functools
import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import psutil
import requests
from send2trash import send2trash

from ..config import DATA_DIR, settings
from ..registry import CONFIRM, CRITICAL, SAFE, P, tool

# ---------------- command risk classification ----------------
_SAFE_HEAD = re.compile(
    r"^(get-\w+|dir|ls|echo|write-output|write-host|whoami|hostname|ipconfig(\s+/all)?|systeminfo|tasklist|"
    r"where(\.exe)?|test-path|test-connection|test-netconnection|ping|tracert|pathping|nslookup|netstat|"
    r"resolve-dnsname|select-string|select-object|where-object|sort-object|format-table|format-list|"
    r"measure-object|out-string|convertto-json|group-object|type|cat|more|driverquery|findstr|"
    r"powercfg\s+/(list|query|a)|netsh\s+(wlan|interface)\s+show|winget\s+(list|search|show)|"
    r"git\s+(status|log|diff|branch|show|remote)|pip\s+(list|show|freeze)|npm\s+(ls|list|view))\b", re.I)
_VERSION = re.compile(r"^[\w.\-]+(\.exe)?\s+(--version|-v|-version|version)\s*$", re.I)
_DANGER = re.compile(
    r"\b(remove-item|rm|del|erase|rd|rmdir|format-volume|diskpart|clear-disk|clear-recyclebin|initialize-disk|"
    r"stop-computer|restart-computer|shutdown|bcdedit|remove-itemproperty|set-executionpolicy|cipher|"
    r"uninstall-\w+|takeown|icacls|disable-\w+|set-mppreference|invoke-expression|iex|reg\s+delete|"
    r"net\s+user|winget\s+uninstall|msiexec\s+/x|vssadmin|wbadmin|remove-\w+)\b|\bformat(\.com)?\s+[a-z]:",
    re.I)


def _regex_level(text: str) -> str:
    if re.search(r"(?<![-=<])>|\$\(|`|&\s*[\"'\w$]|\b(set|add|out|new|copy|move|rename|start|stop|restart|install|"
                 r"invoke|enable|register|update)-\w+", text, re.I):
        return CONFIRM
    segments = [s.strip() for s in re.split(r"\|\||&&|[|;\n]", text) if s.strip()]
    if segments and all(_SAFE_HEAD.match(s) or _VERSION.match(s) for s in segments):
        return SAFE
    return CONFIRM


# ---- precise check with PowerShell's own parser (understands pipelines, script blocks and method calls) ----
_AST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$src = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($env:KARYA_PS_SRC))
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errs)
$cmds = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object {
  $first = $_.CommandElements[0]
  if ($first -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) { $name = '<scriptblock>' }
  else { $name = $_.GetCommandName(); if (-not $name) { $name = '<dynamic>' } }
  @{ name = $name; text = $_.Extent.Text }
})
$members = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true) | ForEach-Object {
  $type = ''
  if ($_.Static -and $_.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { $type = $_.Expression.TypeName.FullName }
  $m = '<dynamic>'
  if ($_.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $m = $_.Member.Value }
  @{ static = [bool]$_.Static; type = $type; member = $m }
})
$redirs = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] }, $true) | ForEach-Object { $_.Location.Extent.Text })
@{ errors = @($errs).Count; commands = $cmds; members = $members; redirects = $redirs } | ConvertTo-Json -Compress -Depth 5
"""
_AST_ENCODED = base64.b64encode(_AST_SCRIPT.encode("utf-16-le")).decode()
_SAFE_CMDLET = re.compile(
    r"^(get|select|sort|measure|group|compare|convertto|convertfrom|test|resolve|join|split|where|foreach|"
    r"format-(table|list|wide|custom|hex)|out-(string|null|host)|write-(output|host|verbose|information|debug)|"
    r"start-sleep|find-(module|package|command))(-|$)", re.I)
_SAFE_ALIASES = {"?", "%", "where", "foreach", "select", "sort", "measure", "group", "ft", "fl", "fw", "gci", "gc",
                 "gps", "gsv", "gi", "gip", "gm", "gwmi", "gcim", "ls", "dir", "cat", "type", "pwd", "echo", "write",
                 "history", "h", "ghy", "gal", "gcm", "gdr", "gl", "gp", "gu", "gv", "sls", "measure-object"}
_BAD_MEMBER = re.compile(r"^(delete|remove|kill|format|clear|uninstall|shutdown|restart|wipe|destroy|drop|truncate|"
                         r"erase|purge|setaccesscontrol|encrypt|decrypt)", re.I)
_SAFE_TYPES = {"math", "system.math", "datetime", "system.datetime", "convert", "system.convert", "string",
               "system.string", "int", "int32", "int64", "long", "double", "decimal", "char", "bool", "regex",
               "system.text.regularexpressions.regex", "guid", "timespan", "io.path", "system.io.path", "version",
               "uri", "bitconverter", "text.encoding", "system.text.encoding", "datetimeoffset", "array", "linq.enumerable"}
_FILE_TYPES = {"io.file", "system.io.file", "io.directory", "system.io.directory"}
_READ_MEMBERS = {"exists", "readalltext", "readalllines", "readallbytes", "getfiles", "getdirectories", "enumeratefiles",
                 "enumeratedirectories", "getlastwritetime", "getcreationtime", "getlastaccesstime", "getattributes",
                 "getcurrentdirectory", "getfolderpath", "getenvironmentvariable", "getlogicaldrives"}
_SAFE_MEMBERS = _READ_MEMBERS | {
    "tostring", "toupper", "tolower", "toupperinvariant", "tolowerinvariant", "trim", "trimstart", "trimend",
    "substring", "split", "replace", "contains", "containskey", "startswith", "endswith", "indexof", "lastindexof",
    "padleft", "padright", "equals", "gettype", "gethashcode", "compareto", "tochararray", "normalize", "insert",
    "toshortdatestring", "tolongdatestring", "toshorttimestring", "tolongtimestring", "tolocaltime",
    "touniversaltime", "adddays", "addhours", "addminutes", "addseconds", "addmonths", "addyears", "round", "floor",
    "ceiling", "abs", "max", "min", "pow", "sqrt", "parse", "tryparse", "match", "matches", "ismatch", "getbytes",
    "getstring", "join", "format", "readtoend", "readline", "getenumerator", "getvalue", "toarray", "tolist",
    "where", "foreach", "count", "sum", "average"}


@functools.lru_cache(maxsize=256)
def _ast_level(text: str) -> str | None:
    env = dict(os.environ, KARYA_PS_SRC=base64.b64encode(text.encode("utf-8")).decode())
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", _AST_ENCODED],
                              capture_output=True, env=env, timeout=20,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(proc.stdout.decode("utf-8", errors="replace").strip() or "null")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not isinstance(info, dict):
        return None
    if info.get("errors"):
        return CONFIRM  # doesn't even parse: let the user look at it
    levels = [SAFE]
    for cmd in info.get("commands") or []:
        name, cmd_text = str(cmd.get("name", "")), str(cmd.get("text", ""))
        low = name.lower()
        if low == "<scriptblock>":
            continue  # & { ... } - its contents are checked separately
        if low == "<dynamic>":
            levels.append(CONFIRM)
        elif _DANGER.search(name) or _DANGER.search(cmd_text):
            levels.append(CRITICAL)
        elif _SAFE_CMDLET.match(name) or low in _SAFE_ALIASES or _SAFE_HEAD.match(cmd_text) or _VERSION.match(cmd_text):
            continue
        else:
            levels.append(CONFIRM)
    for mem in info.get("members") or []:
        member, typ = str(mem.get("member", "")).lower(), str(mem.get("type", "")).lower()
        if _BAD_MEMBER.match(member):
            levels.append(CRITICAL)
        elif mem.get("static") and typ in _SAFE_TYPES:
            continue
        elif mem.get("static") and typ in _FILE_TYPES:
            levels.append(SAFE if member in _READ_MEMBERS else CONFIRM)
        elif member in _SAFE_MEMBERS:
            continue
        else:
            levels.append(CONFIRM)
    for target in info.get("redirects") or []:
        if str(target).strip().lower() not in ("$null", "nul"):
            levels.append(CONFIRM)  # writes a file
    order = {SAFE: 0, CONFIRM: 1, CRITICAL: 2}
    return max(levels, key=order.__getitem__)


def classify_command(command: str) -> str:
    text = command.strip()
    if _DANGER.search(text):
        return CRITICAL
    simple = not re.search(r"[{}()\[\]]|::|`|\$\(", text)  # no script blocks, method calls, types or subexpressions
    if simple and _regex_level(text) == SAFE:
        return SAFE
    level = _ast_level(text)
    if level is not None:
        return level
    return _regex_level(text) if simple else CONFIRM  # parser unavailable: never call a complex command safe


def _cmd_risk(args: dict) -> tuple[str, str]:
    command = str(args.get("command", ""))
    level = CRITICAL if args.get("admin") else classify_command(command)
    where = f" (in {args['cwd']})" if args.get("cwd") else ""
    admin = " as ADMINISTRATOR" if args.get("admin") else ""
    return level, f"Run PowerShell{admin}{where}: {command[:400]}"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n")


_SECRET_ENV = re.compile(r"(API_KEY|_TOKEN|_SECRET|PASSWORD|APP_PASSWORD)$", re.I)


def child_env() -> dict:
    """Environment for commands/scripts Karya runs: everything except Karya's own keys and passwords,
    so a command (or a web page that tricks the AI) can't read them."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


@tool("run_command", "Run a PowerShell command on this PC and return its output. Read-only commands run immediately; "
      "anything that changes the system asks the user first. Use admin=true only for tasks that need elevation "
      "(sfc, DISM, services) - Windows will show a UAC prompt.", {
    "command": P("string", "PowerShell command"),
    "cwd": P("string", "Working directory (default: workspace)"),
    "timeout": P("integer", "Seconds before giving up (default 90, max 900)"),
    "admin": P("boolean", "Run elevated (UAC prompt)"),
}, required=["command"], risk=_cmd_risk, group="pc")
def run_command(command: str, cwd: str | None = None, timeout: int = 90, admin: bool = False):
    timeout = max(5, min(int(timeout), 900))
    workdir = cwd or str(settings.workspace)
    if admin:
        return _run_admin(command, workdir, timeout)
    prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", prefix + command],
                              capture_output=True, cwd=workdir, timeout=timeout, env=child_env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        return {"error": f"timed out after {timeout}s", "partial_output": _decode(exc.stdout or b"")[-4000:]}
    out, err = _decode(proc.stdout), _decode(proc.stderr)
    return {"exit_code": proc.returncode, "output": out[-8000:] or "(no output)", **({"errors": err[-3000:]} if err.strip() else {})}


def _run_admin(command: str, workdir: str, timeout: int):
    scratch = DATA_DIR / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    script, output = scratch / f"admin_{stamp}.ps1", scratch / f"admin_{stamp}.txt"
    script.write_text(f"Set-Location -LiteralPath '{workdir}'\n& {{\n{command}\n}} *> '{output}'\n", encoding="utf-8-sig")
    launcher = (f"Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList "
                f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{script}'")
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-Command", launcher], capture_output=True, timeout=timeout,
                              env=child_env())
        text = ""
        if output.exists():
            raw = output.read_bytes()  # Windows PowerShell 5.1 redirection writes UTF-16 with a BOM
            text = raw.decode("utf-16", errors="replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
                else raw.decode("utf-8", errors="replace")
        if proc.returncode != 0 and not text:
            return {"error": "elevation was cancelled or failed", "details": _decode(proc.stderr)[-1500:]}
        return {"exit_code": proc.returncode, "output": text[-8000:] or "(no output)"}
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    finally:
        for f in (script, output):
            try:
                f.unlink()
            except OSError:
                pass


@tool("run_python", "Run a Python 3 snippet (data processing, calculations, file conversions, automation). "
      "Print what you want to see.", {
    "code": P("string", "Python source code"),
    "timeout": P("integer", "Seconds (default 120)"),
}, required=["code"], risk=CONFIRM, group="pc", summary=lambda a: "Run Python code:\n" + str(a.get("code", ""))[:600])
def run_python(code: str, timeout: int = 120):
    scratch = settings.workspace / ".scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"snippet_{int(time.time() * 1000)}.py"
    path.write_text(code, encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, "-X", "utf8", str(path)], capture_output=True, cwd=settings.workspace,
                              env=child_env(),
                              timeout=max(5, min(int(timeout), 900)))
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout}s"}
    finally:
        path.unlink(missing_ok=True)
    return {"exit_code": proc.returncode, "output": _decode(proc.stdout)[-8000:] or "(no output)",
            **({"errors": _decode(proc.stderr)[-3000:]} if proc.stderr.strip() else {})}


# ---------------- files ----------------
def resolve(path: str) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(path).strip().strip('"')))
    if re.fullmatch(r"[A-Za-z]:", raw):
        raw += "\\"  # "D:" alone is the D: drive itself, not "the current folder on D:"
    p = Path(raw)
    return p if p.is_absolute() else (settings.workspace / p)


def in_workspace(path: Path) -> bool:
    try:
        path.resolve().relative_to(settings.workspace.resolve())
        return True
    except ValueError:
        return False


# Files that hold secrets or credentials: Karya refuses to read them (the AI only needs them via browser_type_secret).
from ..config import ROOT as _ROOT  # noqa: E402

_SECRET_FILES = {p.resolve() for p in (DATA_DIR / "vault.json", DATA_DIR / ".token", _ROOT / ".env",
                                       _ROOT / "karya" / "extension" / "config.json")}
_SECRET_NAMES = re.compile(r"(^|[\\/])(\.env|vault\.json|\.token|id_rsa|id_ed25519|\.pem|\.ppk|\.pfx|\.p12|"
                           r"credentials\.json|secrets?\.(json|ya?ml|txt)|login data|key[0-9]*\.db|logins\.json)$", re.I)
_SECRET_DIRS = re.compile(r"[\\/](\.ssh|\.aws|\.gnupg|\.kiro[\\/]|google[\\/]chrome[\\/]user data|"
                          r"microsoft[\\/]edge[\\/]user data)[\\/]?", re.I)


def secret_file_reason(path: Path) -> str | None:
    """Why Karya shouldn't hand this file's contents to the AI, or None."""
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in _SECRET_FILES or (DATA_DIR.resolve() in resolved.parents and resolved.name in
                                     ("vault.json", ".token")):
        return "Karya's own secrets file"
    text = str(resolved)
    if _SECRET_NAMES.search(text) or _SECRET_DIRS.search(text):
        return "a credentials/secrets file"
    return None


def extract_text(path: str | Path, max_chars: int = 12000) -> str:
    p = resolve(str(path))
    if not p.exists():
        return f"ERROR: file not found: {p}"
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        text = "\n".join((page.extract_text() or "") for page in PdfReader(str(p)).pages[:50])
    elif suffix == ".docx":
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        text = re.sub(r"<[^>]+>", "", xml)
    else:
        raw = p.read_bytes()[: max_chars * 4]
        if b"\x00" in raw[:2048]:
            return f"(binary file, {p.stat().st_size} bytes) - can't show as text"
        text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
    return text


@tool("read_file", "Read a text, code, PDF or Word (.docx) file.", {
    "path": P("string", "File path (relative paths are inside the workspace)"),
    "max_chars": P("integer", "Max characters (default 12000)"),
}, required=["path"], group="files")
def read_file(path: str, max_chars: int = 12000):
    reason = secret_file_reason(resolve(path))
    if reason:
        return (f"ERROR: Karya won't read {reason} ({resolve(path)}). Passwords are used only through "
                "browser_type_secret and are never shown. If you truly need this, the user can open it themselves.")
    return extract_text(path, max_chars)


def _write_risk(args: dict) -> tuple[str, str]:
    p = resolve(str(args.get("path", "")))
    try:
        resolved = p.resolve()
        in_code = _ROOT.resolve() in resolved.parents and (_ROOT / "karya").resolve() in [resolved, *resolved.parents]
    except OSError:
        in_code = False
    if secret_file_reason(p) or in_code or resolved.name == ".env":
        return CRITICAL, f"WARNING: write to {p} - this is one of Karya's own/secret files. Approve only if you meant to."
    new_in_ws = in_workspace(p) and not p.exists()
    verb = "Append to" if args.get("append") else ("Create" if not p.exists() else "OVERWRITE")
    return (SAFE if new_in_ws else CONFIRM), f"{verb} file {p} ({len(str(args.get('content', '')))} chars)"


@tool("write_file", "Create or overwrite a text file (code, notes, letters, CSV...).", {
    "path": P("string", "File path (relative = inside workspace)"),
    "content": P("string", "Full file content"),
    "append": P("boolean", "Append instead of overwrite"),
}, required=["path", "content"], risk=_write_risk, group="files")
def write_file(path: str, content: str, append: bool = False):
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return f"Wrote {len(content)} chars to {p}"


@tool("list_files", "List files and folders in a directory.", {
    "path": P("string", "Folder (default workspace). Shortcuts: ~, ~/Desktop, ~/Downloads, ~/Documents"),
    "pattern": P("string", "Filter like *.pdf"),
    "recursive": P("boolean", "Include subfolders"),
    "limit": P("integer", "Max entries (default 200)"),
}, group="files")
def list_files(path: str = "", pattern: str = "*", recursive: bool = False, limit: int = 200):
    root = resolve(path) if path else settings.workspace
    if not root.exists():
        return f"ERROR: folder not found: {root}"
    iterator = root.rglob(pattern) if recursive else root.glob(pattern)
    rows = []
    for item in iterator:
        try:
            stat = item.stat()
        except OSError:
            continue
        rows.append({"name": str(item.relative_to(root)), "type": "dir" if item.is_dir() else "file",
                     "size_kb": round(stat.st_size / 1024, 1) if item.is_file() else None,
                     "modified": dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")})
        if len(rows) >= limit:
            break
    return {"folder": str(root), "entries": rows}


@tool("find_files", "Search for files by name pattern under a folder (e.g. find my resume, find *.psd on D:).", {
    "pattern": P("string", "Name pattern, e.g. *resume*.pdf"),
    "root": P("string", "Where to search (default your user folder)"),
    "limit": P("integer", "Max results (default 50)"),
}, required=["pattern"], group="files")
def find_files(pattern: str, root: str = "~", limit: int = 50):
    base = resolve(root)
    skip = {"node_modules", ".git", "appdata", "$recycle.bin", "windows", ".venv", "site-packages"}
    found, deadline = [], time.time() + 25
    pat = pattern.lower()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in skip and not d.startswith(".")]
        for name in filenames + dirnames:
            if fnmatch.fnmatch(name.lower(), pat):
                found.append(os.path.join(dirpath, name))
                if len(found) >= limit:
                    return found
        if time.time() > deadline:
            found.append("...(stopped after 25s; narrow the root folder)")
            break
    return found or f"No matches for {pattern} under {base}"


@tool("make_folder", "Create a folder.", {"path": P("string", "Folder path")}, required=["path"], group="files")
def make_folder(path: str):
    p = resolve(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Folder ready: {p}"


@tool("copy_path", "Copy a file or folder.", {"source": P("string", "From"), "destination": P("string", "To")},
      required=["source", "destination"], risk=CONFIRM, group="files")
def copy_path(source: str, destination: str):
    import shutil
    src, dst = resolve(source), resolve(destination)
    if src.is_dir():
        shutil.copytree(src, dst / src.name if dst.is_dir() else dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return f"Copied {src} -> {dst}"


@tool("move_path", "Move or rename a file or folder.", {"source": P("string", "From"), "destination": P("string", "To")},
      required=["source", "destination"], risk=CONFIRM, group="files")
def move_path(source: str, destination: str):
    import shutil
    src, dst = resolve(source), resolve(destination)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Moved {src} -> {dst}"


@tool("delete_path", "Delete a file or folder (goes to the Recycle Bin, so it can be restored).", {
    "path": P("string", "File or folder"),
}, required=["path"], risk=CRITICAL, group="files", summary=lambda a: f"Move to Recycle Bin: {resolve(str(a.get('path', '')))}")
def delete_path(path: str):
    p = resolve(path)
    if not p.exists():
        return f"ERROR: not found: {p}"
    send2trash(str(p))
    return f"Moved to Recycle Bin: {p}"


_EXEC_EXT = {".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js", ".jar", ".scr", ".com", ".reg", ".lnk"}


def _open_risk(args: dict) -> tuple[str, str]:
    target = str(args.get("target", ""))
    if re.match(r"^https?://", target, re.I):
        return SAFE, f"Open {target} in your default browser"
    p = resolve(target)
    if p.exists() and p.suffix.lower() not in _EXEC_EXT:
        return SAFE, f"Open {p}"
    return CONFIRM, f"Launch program: {target}"


@tool("open_path", "Open a file, folder, URL or app on the PC (e.g. 'notepad', 'calc', 'D:/report.pdf', 'https://...').", {
    "target": P("string", "Path, URL or program name"),
}, required=["target"], risk=_open_risk, group="pc")
def open_path(target: str):
    if re.match(r"^https?://", target, re.I):
        os.startfile(target)  # noqa: S606 - opening a URL in the user's browser
        return f"Opened {target}"
    p = resolve(target)
    if p.exists():
        os.startfile(str(p))
        return f"Opened {p}"
    subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", "Start-Process", target],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"Launched {target}"


# ---------------- system ----------------
@tool("system_info", "PC health: OS, CPU, RAM, disks, battery, uptime, top memory users.", group="pc")
def system_info():
    vm = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"drive": part.mountpoint, "total_gb": round(u.total / 1e9, 1),
                          "free_gb": round(u.free / 1e9, 1), "used_pct": u.percent})
        except (PermissionError, OSError):
            continue
    battery = psutil.sensors_battery()
    boot = dt.datetime.fromtimestamp(psutil.boot_time())
    return {"os": f"{sys.platform} {os.environ.get('OS', '')}", "computer": socket.gethostname(),
            "cpu_percent": psutil.cpu_percent(interval=0.5), "cpu_cores": psutil.cpu_count(),
            "ram_total_gb": round(vm.total / 1e9, 1), "ram_used_pct": vm.percent,
            "disks": disks, "battery": ({"percent": battery.percent, "plugged_in": battery.power_plugged} if battery else None),
            "uptime_hours": round((dt.datetime.now() - boot).total_seconds() / 3600, 1),
            "top_memory": list_processes(limit=6)}


@tool("list_processes", "List running programs sorted by memory or CPU use.", {
    "sort_by": P("string", "memory or cpu", enum=["memory", "cpu"]),
    "limit": P("integer", "How many (default 15)"),
    "name_filter": P("string", "Only processes whose name contains this"),
}, group="pc")
def list_processes(sort_by: str = "memory", limit: int = 15, name_filter: str = ""):
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            name = p.info["name"] or ""
            if name_filter and name_filter.lower() not in name.lower():
                continue
            procs.append({"pid": p.info["pid"], "name": name,
                          "memory_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1e6, 1),
                          "cpu": p.info["cpu_percent"]})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = "cpu" if sort_by == "cpu" else "memory_mb"
    return sorted(procs, key=lambda r: r[key] or 0, reverse=True)[:limit]


@tool("kill_process", "Close a running program by name or PID.", {
    "target": P("string", "Process name (e.g. chrome.exe) or PID"),
}, required=["target"], risk=CONFIRM, group="pc", summary=lambda a: f"Force-close process: {a.get('target')}")
def kill_process(target: str):
    killed = []
    for p in psutil.process_iter(["pid", "name"]):
        if str(p.info["pid"]) == str(target) or (p.info["name"] or "").lower() == str(target).lower():
            try:
                p.terminate()
                killed.append(f"{p.info['name']} ({p.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                killed.append(f"failed {p.info['pid']}: {exc}")
    return killed or f"No process matching {target}"


@tool("network_check", "Diagnose internet: ping, DNS and HTTPS checks with timings.", group="pc")
def network_check():
    result = {}
    t = time.time()
    try:
        socket.gethostbyname("www.google.com")
        result["dns"] = f"ok ({(time.time() - t) * 1000:.0f} ms)"
    except OSError as exc:
        result["dns"] = f"FAILED: {exc}"
    t = time.time()
    try:
        requests.get("https://www.google.com/generate_204", timeout=8)
        result["https"] = f"ok ({(time.time() - t) * 1000:.0f} ms)"
    except requests.RequestException as exc:
        result["https"] = f"FAILED: {type(exc).__name__}"
    ping = subprocess.run(["ping", "-n", "3", "8.8.8.8"], capture_output=True, timeout=20)
    result["ping_8.8.8.8"] = _decode(ping.stdout).strip().splitlines()[-1:] or ["no reply"]
    addrs = psutil.net_if_addrs()
    result["adapters_up"] = [n for n, s in psutil.net_if_stats().items() if s.isup and n in addrs][:8]
    return result


@tool("security_check", "Check how locked-down Karya is on this PC: who can reach it, whether passwords are encrypted "
      "and secret files are unreadable, and which automatic modes are on. Plain-language report.", group="pc")
def security_check():
    from ..config import DATA_DIR, ROOT, settings
    checks, warnings = [], []
    checks.append("Karya listens only on 127.0.0.1 (this PC); other devices and websites can't reach it.")
    try:
        token = (DATA_DIR / ".token").read_text(encoding="utf-8").strip()
        checks.append(f"The chat link is protected by a {len(token)}-character secret token.")
    except OSError:
        warnings.append("No access token file found.")
    checks.append("Every API call and WebSocket checks the token and the Host/Origin headers.")
    vault_file = DATA_DIR / "vault.json"
    if vault_file.exists():
        try:
            import json as _json
            raw = _json.loads(vault_file.read_text(encoding="utf-8"))
            leaked = [s for s, a in raw.get("accounts", {}).items()
                      if a.get("secret") and len(a["secret"]) < 24]
            checks.append("Saved passwords are encrypted with Windows DPAPI (only your Windows account can decrypt them)."
                          if not leaked else "")
            if leaked:
                warnings.append("Some vault entries look unencrypted: " + ", ".join(leaked))
        except (OSError, ValueError):
            warnings.append("The vault file couldn't be read as JSON.")
    else:
        checks.append("No vault file yet (no saved passwords).")
    for probe, label in ((DATA_DIR / "vault.json", "vault"), (ROOT / ".env", ".env")):
        if read_file(str(probe)).startswith("ERROR: Karya won't read"):
            checks.append(f"The AI cannot read Karya's {label} file; it's blocked.")
        elif probe.exists():
            warnings.append(f"The {label} file was readable - that shouldn't happen.")
    checks.append("Tool output is scrubbed of API keys, the token and the vault before the AI or logs see it.")
    checks.append("Commands and scripts run without Karya's keys/passwords in their environment.")
    modes = []
    if settings.full_access:
        modes.append("Full access is ON" + (" including payments/deletes" if settings.full_access_payments
                     else " (payments and account deletes still ask)"))
    if settings.approval_mode == "auto":
        modes.append("'ask only for critical actions' is on")
    if settings.reuse_login:
        modes.append("one login is reused across sites")
    if settings.mcp_enabled:
        modes.append("other AI apps may use Karya over MCP (with the token)")
    checks.append("Automatic modes: " + ("; ".join(modes) if modes else "none - Karya asks before risky actions."))
    return {"secure": checks, "warnings": warnings or ["none"],
            "limits_per_day": {"emails": settings.max_emails_per_day, "posts": settings.max_posts_per_day,
                               "applications": settings.max_applications_per_day}}


@tool("clipboard", "Read or set the Windows clipboard.", {
    "action": P("string", "get or set", enum=["get", "set"]),
    "text": P("string", "Text to copy (for set)"),
}, required=["action"], group="pc")
def clipboard(action: str, text: str = ""):
    if action == "set":
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                       input=text.encode("utf-8"), capture_output=True, timeout=15)
        return "Copied to clipboard."
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                          "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                         capture_output=True, timeout=15)
    return _decode(out.stdout)[:8000] or "(clipboard is empty)"
