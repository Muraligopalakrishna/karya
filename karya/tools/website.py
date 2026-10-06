"""Build websites in the workspace, preview them locally and deploy to Vercel."""
from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

from ..config import settings
from ..registry import CRITICAL, P, tool

VERCEL_CLI = "vercel@62.0.0"
_servers: dict[str, tuple[http.server.ThreadingHTTPServer, int]] = {}


def sites_dir() -> Path:
    d = settings.workspace / "sites"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "site")[:50]


def _safe_join(root: Path, rel: str) -> Path:
    target = (root / rel.replace("\\", "/").lstrip("/")).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError(f"path escapes the site folder: {rel}")
    return target


@tool("website_create", "Create or update a website: write its files (index.html, style.css, script.js, images...). "
      "Write complete, modern, responsive code.", {
    "name": P("string", "Site name, e.g. 'my-portfolio'"),
    "files": P("object", "Map of relative file path -> full file content, e.g. {\"index.html\": \"<!doctype html>...\"}",
               additionalProperties={"type": "string"}),
}, required=["name", "files"], group="website", summary=lambda a: f"Write site '{a.get('name')}' files: {list((a.get('files') or {}) if isinstance(a.get('files'), dict) else [])}")
def website_create(name: str, files):
    root = sites_dir() / slug(name)
    root.mkdir(parents=True, exist_ok=True)
    if isinstance(files, list):  # also accept [{"path":..., "content":...}]
        files = {f.get("path"): f.get("content", "") for f in files if isinstance(f, dict)}
    written = []
    for rel, content in (files or {}).items():
        target = _safe_join(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target.relative_to(root)))
    return {"site": slug(name), "folder": str(root), "files_written": written,
            "next": "website_preview to view it, website_deploy to publish."}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the console clean
        pass


@tool("website_preview", "Serve a site locally (http://127.0.0.1:port) and optionally open it in the default browser.", {
    "name": P("string", "Site name"),
    "open_in_browser": P("boolean", "Open it for the user (default true)"),
}, required=["name"], group="website")
def website_preview(name: str, open_in_browser: bool = True):
    root = sites_dir() / slug(name)
    if not (root / "index.html").exists():
        return f"ERROR: {root} has no index.html. Create the site first."
    if slug(name) in _servers:
        port = _servers[slug(name)][1]
    else:
        port = _free_port()
        handler = functools.partial(_QuietHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True, name=f"preview-{slug(name)}").start()
        _servers[slug(name)] = (server, port)
    url = f"http://127.0.0.1:{port}/"
    if open_in_browser:
        try:
            os.startfile(url)
        except OSError:
            pass
    return {"url": url, "folder": str(root), "tip": "Use browser_open on this URL + browser_screenshot to check the design."}


def stop_previews() -> None:
    for server, _ in list(_servers.values()):
        server.shutdown()
    _servers.clear()


@tool("website_list", "List websites in the workspace.", group="website")
def website_list():
    rows = []
    for d in sorted(sites_dir().iterdir()):
        if d.is_dir():
            files = [str(p.relative_to(d)) for p in d.rglob("*") if p.is_file() and ".vercel" not in p.parts]
            rows.append({"site": d.name, "files": files[:30], "preview": _servers.get(d.name, (None, None))[1]})
    return rows or "No sites yet."


@tool("website_deploy", "Publish a site to the internet with Vercel and return its live URL.", {
    "name": P("string", "Site name"),
    "production": P("boolean", "Deploy to the production URL (default true)"),
}, required=["name"], risk=lambda a: (CRITICAL, f"Publish site '{slug(str(a.get('name', '')))}' publicly on Vercel"),
      group="website")
def website_deploy(name: str, production: bool = True):
    root = sites_dir() / slug(name)
    if not root.exists():
        return f"ERROR: no site named {slug(name)}"
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return "ERROR: Node.js/npx not found. Install Node.js from https://nodejs.org"
    cmd = [npx, "--yes", VERCEL_CLI, "deploy", "--yes"]
    if production:
        cmd.append("--prod")
    if settings.vercel_token:
        cmd += ["--token", settings.vercel_token]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, timeout=600,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "ERROR: deploy timed out after 10 minutes"
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    if settings.vercel_token:
        out = out.replace(settings.vercel_token, "***")
    urls = re.findall(r"https://[\w.-]+\.vercel\.app\S*", out)
    if proc.returncode != 0:
        hint = ""
        if re.search(r"credentials|log ?in|token", out, re.I):
            hint = (" Not logged in to Vercel: add VERCEL_TOKEN to .env (https://vercel.com/account/tokens) "
                    "or run 'npx vercel login' once in a terminal.")
        return f"ERROR: deploy failed (exit {proc.returncode}).{hint}\n{out[-2000:]}"
    return {"live_urls": list(dict.fromkeys(urls)), "log_tail": out[-800:]}
