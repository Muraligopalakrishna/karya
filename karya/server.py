"""Chat UI server. Binds to 127.0.0.1 only; every API/WebSocket call needs the per-launch secret token,
and Host/Origin headers are checked so other websites can't talk to your agent."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import sys
import uuid
import webbrowser

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import tools  # noqa: F401 - registers tools
from .agent import Agent
from .config import DATA_DIR, ROOT, settings
from .registry import TOOLS

STATIC = ROOT / "karya" / "static"
UI_FILES = ("index.html", "app.js", "style.css")
OUTDATED_PAGE = ("Karya was updated, but this page is the old version. Press F5 to reload it. (The old page showed "
                 "Karya's question cards as a login form. Anything you typed there was thrown away: it was not saved or "
                 "sent anywhere.)")


def ui_version() -> str:
    """Changes whenever the chat page's files change, so open pages know to reload."""
    digest = hashlib.sha256()
    for name in UI_FILES:
        try:
            digest.update((STATIC / name).read_bytes())
        except OSError:
            pass
    return digest.hexdigest()[:12]


def index_html(version: str) -> str:
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    text = text.replace('href="/static/style.css"', f'href="/static/style.css?v={version}"')
    text = text.replace('src="/static/app.js"', f'src="/static/app.js?v={version}"')
    return text.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  <meta name="karya-ui" content="{version}">', 1)


class Hub:
    """Connects the single agent to every open Karya tab (all of them see the same chat and approval cards)."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.asks: dict[str, tuple[asyncio.Future, dict]] = {}
        self.task: asyncio.Task | None = None
        self.mcp_client: str | None = None   # the AI app (over MCP) whose tool call is running right now

    @property
    def ws(self) -> WebSocket | None:  # kept for older callers/tests
        return next(iter(self.clients), None)

    async def send(self, event: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                self.clients.discard(ws)

    async def confirm(self, request: dict) -> bool:
        # Tool-call ids can repeat across turns (or be empty) with some providers, so every approval
        # request gets its own id; the card and the answer are matched on that.
        request = {**request, "call_id": request.get("id"), "id": uuid.uuid4().hex}
        future = asyncio.get_running_loop().create_future()
        self.pending[request["id"]] = (future, request)
        await self.send({"type": "confirm", **request})
        from .ext_link import link
        link.notify("attention", {"on": True, "text": str(request.get("summary") or "approval needed")[:150]})
        try:
            approved = await future
            await self.send({"type": "confirm_done", "id": request["id"], "approved": bool(approved)})
            return approved
        finally:
            self.pending.pop(request["id"], None)
            if not self.pending and not self.asks:
                link.notify("attention", {"on": False})

    def answer(self, request_id: str, approved: bool) -> None:
        entry = self.pending.get(request_id)
        if entry and not entry[0].done():
            entry[0].set_result(bool(approved))

    def deny_all(self) -> None:
        for future, _ in list(self.pending.values()):
            if not future.done():
                future.set_result(False)
        for future, _ in list(self.asks.values()):
            if not future.done():
                future.set_result(None)

    async def ask(self, request: dict) -> dict | None:
        """Secure form (e.g. a login). The answer goes to the agent's vault code, never into the chat history."""
        request = {**request, "call_id": request.get("id"), "id": uuid.uuid4().hex}
        future = asyncio.get_running_loop().create_future()
        self.asks[request["id"]] = (future, request)
        await self.send({"type": "ask", **request})
        from .ext_link import link
        link.notify("attention", {"on": True, "text": "Karya is asking you something in its chat"})
        try:
            answer = await future
            await self.send({"type": "ask_done", "id": request["id"], "answered": bool(answer)})
            return answer
        finally:
            self.asks.pop(request["id"], None)
            if not self.pending and not self.asks:
                link.notify("attention", {"on": False})

    def answer_ask(self, request_id: str, data: dict | None) -> None:
        entry = self.asks.get(request_id)
        if entry and not entry[0].done():
            entry[0].set_result(data)

    async def start_run(self, text: str) -> None:
        async def runner():
            await self.send({"type": "busy", "value": True})
            try:
                await self.agent.run(text, self.send, self.confirm, self.ask)
            except Exception as exc:  # keep the server alive whatever happens
                await self.send({"type": "error", "text": f"Internal error: {type(exc).__name__}: {exc}"})
            finally:
                await self.send({"type": "busy", "value": False})
        self.task = asyncio.create_task(runner())


def create_app(agent: Agent | None = None, token: str | None = None, port: int | None = None,
               extra_hosts: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="Karya", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    app.state.port = port or settings.port
    app.state.hub = Hub(agent or Agent())
    hosts = {f"127.0.0.1:{app.state.port}", f"localhost:{app.state.port}", *(extra_hosts or set())}
    origins = {f"http://127.0.0.1:{app.state.port}", f"http://localhost:{app.state.port}",
               *(f"http://{h}" for h in (extra_hosts or set()))}
    from .ext_link import extension_id
    ext_id = extension_id()
    ext_origins = {f"chrome-extension://{ext_id}"} if ext_id else set()

    def token_ok(value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(value, app.state.token)

    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return PlainTextResponse("Forbidden host", status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-cache"  # always revalidate: an update must never leave old JS running
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        return response

    @app.get("/")
    async def index():
        return HTMLResponse(index_html(ui_version()))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------------------------------------------------------------- other AI apps (MCP bridge, karya_mcp.py)
    from .mcp_bridge import McpRuns, setup_info, tool_list
    app.state.mcp = McpRuns(app.state.hub, f"http://127.0.0.1:{app.state.port}/?token={app.state.token}")

    def bridge_ok(request: Request) -> bool:
        """Only the local MCP bridge: the install's token in the Authorization header, and no web page origin."""
        auth = request.headers.get("authorization", "")
        return auth.lower().startswith("bearer ") and token_ok(auth[7:].strip()) and not request.headers.get("origin")

    @app.get("/api/mcp/info")
    async def mcp_info(request: Request, tools: str = ""):
        if not bridge_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import __version__
        return {"enabled": settings.mcp_enabled, "version": __version__, "tools": tool_list(tools)}

    @app.post("/api/mcp/call")
    async def mcp_call(request: Request):
        if not bridge_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = dict(await request.json())
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return await app.state.mcp.call(body)

    @app.post("/api/mcp/resume")
    async def mcp_resume(request: Request):
        if not bridge_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = dict(await request.json())
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        return await app.state.mcp.resume(body)

    @app.post("/api/mcp/cancel")
    async def mcp_cancel(request: Request):
        if not bridge_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = dict(await request.json())
        except (ValueError, TypeError):
            body = {}
        return app.state.mcp.cancel(body)

    @app.get("/api/mcp/setup")
    async def mcp_setup(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return {**setup_info(ROOT), "enabled": settings.mcp_enabled, "last_client": app.state.mcp.last_client}

    @app.get("/api/status")
    async def status(token: str = ""):
        if not token_ok(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        hub: Hub = app.state.hub
        checks = {}
        path = DATA_DIR / "cache" / "provider_checks.json"
        if path.exists():
            import json as _json
            try:
                checks = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                checks = {}
        from .ext_link import EXTENSION_DIR, link
        return {**settings.status(), "tools": len(TOOLS), "auto_mode": hub.agent.auto_mode, "busy": hub.agent.busy,
                "browser_link": link.status(), "extension_dir": str(EXTENSION_DIR), "browser_mode": settings.browser_mode,
                "checks": {k: {f: v.get(f) for f in ("ok", "model", "plan", "tokens_per_minute", "summary", "error")}
                           for k, v in checks.items() if k in {p.name for p in settings.providers}}}

    def allowed(request: Request, token: str) -> bool:
        origin = request.headers.get("origin")
        return token_ok(token) and (origin is None or origin in origins)

    @app.get("/api/settings")
    async def get_settings(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from .config import settings_view
        return settings_view()

    @app.post("/api/settings")
    async def post_settings(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from .config import PRESETS, update_env
        from .llm import LLMClient
        try:
            body = await request.json()
            changed = update_env({k: v for k, v in dict(body).items() if v is not None})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        hub: Hub = app.state.hub
        hub.agent.llm = LLMClient(settings.providers)  # new keys take effect for the next message
        if "APPROVAL_MODE" in changed:
            hub.agent.auto_mode = settings.approval_mode == "auto"
        touched = {p.name for p in PRESETS if p.key_env in changed or f"{p.name.upper()}_MODEL" in changed}
        if {"CUSTOM_BASE_URL", "CUSTOM_API_KEY", "CUSTOM_MODEL"} & set(changed):
            touched.add("custom")
        checks = await asyncio.to_thread(_check_providers, hub.agent.llm, touched)
        return {"changed": changed, "status": settings.status(), "checks": checks}

    @app.get("/api/providers/check")
    async def check_providers(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        hub: Hub = app.state.hub
        names = {p.name for p in hub.agent.llm.providers if not p.slow}
        return {"checks": await asyncio.to_thread(_check_providers, hub.agent.llm, names)}

    @app.get("/api/accounts")
    async def get_accounts(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import vault
        return {"accounts": vault.list_accounts()}

    @app.post("/api/accounts")
    async def post_account(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import vault
        try:
            body = dict(await request.json())
            saved = await asyncio.to_thread(vault.save_account, str(body.get("site", "")), str(body.get("username", "")),
                                            str(body.get("password") or "") or None, str(body.get("notes", ""))[:200])
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"saved": saved}

    @app.delete("/api/accounts")
    async def delete_account(request: Request, token: str = "", site: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import vault
        return {"deleted": vault.delete_account(site)}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        origin = websocket.headers.get("origin", "")
        if not token_ok(websocket.query_params.get("token")) or (origin and origin not in origins) \
                or websocket.headers.get("host", "") not in hosts:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        hub: Hub = app.state.hub
        hub.clients.add(websocket)
        await websocket.send_json({"type": "history", "messages": hub.agent.visible_history(),
                                   "busy": hub.agent.busy, "auto_mode": hub.agent.auto_mode, "keep_going": settings.keep_going})
        page_version, current = websocket.query_params.get("ui", ""), ui_version()
        if page_version and page_version != current:
            await websocket.send_json({"type": "reload"})
        elif not page_version and "Mozilla" in websocket.headers.get("user-agent", ""):
            await websocket.send_json({"type": "error", "text": OUTDATED_PAGE})  # an old page can't reload itself
        for _, request in list(hub.pending.values()):  # re-show approvals after a page reload
            await websocket.send_json({"type": "confirm", **request})
        for _, request in list(hub.asks.values()):
            await websocket.send_json({"type": "ask", **request})
        try:
            while True:
                data = await websocket.receive_json()
                kind = data.get("type")
                if kind == "chat":
                    text = str(data.get("text", "")).strip()
                    if not text:
                        continue
                    if hub.agent.busy:
                        await hub.send({"type": "error", "text": "I'm still working on the last request. Press Stop first."})
                        continue
                    if hub.mcp_client:
                        await hub.send({"type": "error", "text": f"{hub.mcp_client} is using Karya right now. Try again in "
                                                                 "a moment (or stop it in that app)."})
                        continue
                    await hub.start_run(text[:20000])
                elif kind == "confirm":
                    hub.answer(str(data.get("id")), bool(data.get("approved")))
                elif kind == "ask_reply":
                    entry = hub.asks.get(str(data.get("id")))
                    asked_kind = (entry[1].get("kind") if entry else "") or ""
                    if (asked_kind == "questions" and not data.get("cancel") and not isinstance(data.get("answers"), dict)
                            and (data.get("username") or data.get("password"))):
                        # An old page drew the question card as a login form. Drop what was typed (never saved or
                        # passed on) and keep the question open for the reloaded page.
                        await websocket.send_json({"type": "error", "text": OUTDATED_PAGE})
                        continue
                    if data.get("cancel"):
                        reply = None
                    else:
                        raw_answers = data.get("answers") if isinstance(data.get("answers"), dict) else {}
                        reply = {"username": str(data.get("username", ""))[:300],
                                 "password": str(data.get("password", ""))[:500],
                                 "picked": [str(x)[:10] for x in (data.get("picked") or [])][:50],
                                 "skip_companies": [str(x)[:120] for x in (data.get("skip_companies") or [])][:50],
                                 "answers": {str(k)[:200]: str(v)[:500] for k, v in list(raw_answers.items())[:20]}}
                        if isinstance(data.get("auto_submit"), bool):
                            reply["auto_submit"] = data["auto_submit"]
                    hub.answer_ask(str(data.get("id")), reply)
                elif kind == "stop":
                    hub.agent.cancel()
                    hub.deny_all()
                elif kind == "reset":
                    if hub.agent.busy:
                        hub.agent.cancel()
                        hub.deny_all()
                    hub.agent.reset()
                    await hub.send({"type": "history", "messages": [], "busy": False, "auto_mode": hub.agent.auto_mode, "keep_going": settings.keep_going})
                elif kind == "set_keep_going":
                    settings.keep_going = bool(data.get("on"))
                    try:
                        from .config import update_env
                        await asyncio.to_thread(update_env, {"KEEP_GOING": "true" if settings.keep_going else "false"})
                    except (OSError, ValueError):
                        pass  # still applies to this session
                    await hub.send({"type": "keep_going", "on": settings.keep_going})
                elif kind == "set_mode":
                    hub.agent.auto_mode = bool(data.get("auto"))
                    try:
                        from .config import update_env
                        await asyncio.to_thread(update_env, {"APPROVAL_MODE": "auto" if hub.agent.auto_mode else "ask"})
                    except (OSError, ValueError):
                        pass  # still applies to this session
                    await hub.send({"type": "mode", "auto_mode": hub.agent.auto_mode, "keep_going": settings.keep_going})
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    @app.websocket("/ext")
    async def ext_endpoint(websocket: WebSocket):
        """Karya Browser Link (the extension in the user's own Chrome). Only that extension, with the token."""
        from .ext_link import link
        origin = websocket.headers.get("origin", "")
        if not token_ok(websocket.query_params.get("token")) or websocket.headers.get("host", "") not in hosts \
                or not (origin in ext_origins or (not ext_origins and re.fullmatch(r"chrome-extension://[a-p]{32}", origin))):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError):
            await websocket.close(code=1008)
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await websocket.close(code=1008)
            return
        ua = str(hello.get("ua", ""))
        browser = next((name for pattern, name in ((r"Edg/", "Microsoft Edge"), (r"OPR/", "Opera"), (r"Chrome/", "Chrome"))
                        if re.search(pattern, ua)), "Chromium")
        link.attach(websocket, asyncio.get_running_loop(), {"version": str(hello.get("version", ""))[:20], "browser": browser})
        hub: Hub = app.state.hub
        await hub.send({"type": "browser_link", **link.status()})
        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif data.get("type") == "event":
                    link.event(str(data.get("text", "")))
                elif "id" in data:
                    link.deliver(data)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            link.detach(websocket)
            await hub.send({"type": "browser_link", **link.status()})

    return app


def _check_providers(llm, names: set[str]) -> list[dict]:
    """Probe the given providers (key works? which model? free or paid limits?) and remember the result."""
    import json as _json
    import time as _time
    from .llm import probe_provider
    results = []
    for p in llm.providers:
        if p.name in names and not p.slow:
            try:
                results.append(probe_provider(llm, p))
            except Exception as exc:  # noqa: BLE001 - never break saving settings
                results.append({"provider": p.name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]})
    if results:
        path = DATA_DIR / "cache" / "provider_checks.json"
        try:
            saved = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            saved = {}
        for r in results:
            saved[r["provider"]] = {**r, "checked": int(_time.time())}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(saved, indent=1), encoding="utf-8")
    return results


def load_token() -> str:
    """Per-install secret kept in data/.token so the same bookmarked link keeps working."""
    path = DATA_DIR / ".token"
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 24:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


def _karya_already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
            return b"<title>Karya</title>" in resp.read(4000)
    except OSError:
        return False


def _port_free(port: int) -> bool:
    import socket
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> None:
    import uvicorn
    port = settings.port
    token = load_token()
    url = f"http://127.0.0.1:{port}/?token={token}"
    open_browser = "--no-browser" not in sys.argv
    if _karya_already_running(port):
        print("\n  Karya is already running. Chat page: " + url + "\n", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    if not _port_free(port):
        print(f"\n  Port {port} is used by another program. Set a different PORT in .env and start again.\n", flush=True)
        sys.exit(1)
    app = create_app(token=token, port=port)
    try:
        from .ext_link import write_extension_config
        write_extension_config(port, token)  # lets Karya Browser Link (in your own Chrome) connect by itself
    except OSError as exc:
        print(f"  (Couldn't write the browser extension's config: {exc})", flush=True)
    print("\n  Karya is running. Open this link (keep it private):\n  " + url + "\n", flush=True)
    print("  AI providers: " + (", ".join(p.label for p in settings.providers) or "NONE - add a key to .env"), flush=True)
    print("  Keep this window open while you use Karya. Close it to stop Karya.\n", flush=True)
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
