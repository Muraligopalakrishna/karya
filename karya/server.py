"""Chat UI server. Binds to 127.0.0.1 only; every API/WebSocket call needs the per-launch secret token,
and Host/Origin headers are checked so other websites can't talk to your agent."""
from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import subprocess
import sys
import time
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


MAX_BOTS = 2   # bots working at the same time (each step uses the user's AI plan)


class Hub:
    """Connects the chat's agent and the user's bots to every open Karya tab (all of them see the same chat and
    approval cards). The chat runs one task at a time; bots run on their own agents, in parallel with it."""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.clients: set[WebSocket] = set()
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.asks: dict[str, tuple[asyncio.Future, dict]] = {}
        self.task: asyncio.Task | None = None
        self.mcp_client: str | None = None   # the AI app (over MCP) whose tool call is running right now
        self.source, self.label = "chat", "the chat"   # who started the running task: chat, phone, agent:<id>
        self.queue: list[dict] = []          # tasks that came in (WhatsApp) while the chat was busy
        self.listeners: list = []            # WhatsApp and the bots' report keeper: on_confirm, on_ask, on_done...
        self.bots: dict[str, dict] = {}      # bot id -> its run in progress
        self.bot_queue: list[dict] = []      # bot tasks waiting for the bot (or a free slot)
        self.bot_agents: dict[str, Agent] = {}
        self.make_bot = self._make_bot       # tests swap in a fake agent

    def _make_bot(self, record: dict) -> Agent:
        from . import scheduler
        return Agent(llm=self.agent.llm, history_file=scheduler.history_path(record["id"]), bot=record)

    def set_llm(self, llm) -> None:
        """A new key or model: the chat and every bot use it from their next step."""
        self.agent.llm = llm
        for bot in self.bot_agents.values():
            bot.llm = llm

    # ---------------------------------------------------------------- bots
    def bot_running(self, agent_id: str) -> bool:
        return agent_id in self.bots

    def bot_state(self, agent_id: str) -> dict | None:
        entry = self.bots.get(agent_id)
        queued = sum(1 for item in self.bot_queue if item["agent_id"] == agent_id)
        if entry is None:
            return {"queued": queued} if queued else None
        return {"doing": entry["task"][:200], "since": int(time.time() - entry["started"]), "queued": queued}

    def bots_running(self) -> list[dict]:
        return [{"run": e["run"].id, "bot": e["run"].bot, "agent_id": aid, "task": e["task"][:300]}
                for aid, e in self.bots.items()]

    async def assign(self, agent_id: str, task: str | None, source: str = "assigned",
                     user_words: str | None = None) -> str:
        """Give a bot a task (None = its own job). Returns 'started', or 'queued' while the bot is busy or
        MAX_BOTS bots are working."""
        from . import scheduler
        record = scheduler.find(agent_id)
        if record is None:
            raise ValueError(f"no bot {agent_id!r}")
        item = {"agent_id": record["id"], "task": task, "source": source, "user_words": user_words}
        if record["id"] in self.bots or len(self.bots) >= MAX_BOTS:
            self.bot_queue.append(item)
            return "queued"
        await self._start_bot(record, item)
        return "started"

    async def _start_bot(self, record: dict, item: dict) -> None:
        from . import runctx, scheduler
        agent = self.bot_agents.get(record["id"]) or self.make_bot(record)
        self.bot_agents[record["id"]] = agent
        task_now = item["task"] or record["task"]
        agent.bot = {**record, "_task_now": task_now}
        agent.auto_mode = getattr(self.agent, "auto_mode", False)
        run = runctx.Run(id="b" + uuid.uuid4().hex[:8], source=item["source"], bot=record["name"],
                         agent_id=record["id"])
        entry = {"run": run, "task": task_now, "started": time.time(), "agent": agent}
        self.bots[record["id"]] = entry
        text = scheduler.run_text(record, item["task"], item["source"])

        async def emit(event: dict) -> None:
            if event.get("type") != "busy":                 # the chat's busy state is the chat's own
                await self.send({**event, "run": run.id, "bot": run.bot})

        async def runner():
            runctx.RUN.set(run)
            await self.send({"type": "bot_started", "run": run.id, "bot": run.bot, "agent_id": record["id"],
                             "task": task_now[:300], "source": item["source"]})
            report, ok = "", True
            try:
                report = await agent.run(text, emit, self.confirm, self.ask, user_words=item.get("user_words")) or ""
            except Exception as exc:  # noqa: BLE001 - one bot failing never stops Karya
                ok, report = False, f"{run.bot} stopped because of an internal error: {type(exc).__name__}: {exc}"
            finally:
                self.bots.pop(record["id"], None)
                from . import desk
                desk.DESK.release(run.id)
                await self.send({"type": "bot_done", "run": run.id, "bot": run.bot, "agent_id": record["id"],
                                 "task": task_now[:300], "report": report, "ok": ok})
                await self._tell("on_bot_done", record, task_now, report, item["source"])
                await self._next_bot()
        entry["future"] = asyncio.create_task(runner())

    async def _next_bot(self) -> None:
        from . import scheduler
        while self.bot_queue and len(self.bots) < MAX_BOTS:
            index = next((i for i, it in enumerate(self.bot_queue) if it["agent_id"] not in self.bots), None)
            if index is None:
                return
            item = self.bot_queue.pop(index)
            record = scheduler.find(item["agent_id"])
            if record is not None:
                await self._start_bot(record, item)

    def stop_bot(self, agent_id: str) -> bool:
        """Stop a bot's current work and drop its waiting tasks. True if there was something to stop."""
        dropped = [it for it in self.bot_queue if it["agent_id"] == agent_id]
        self.bot_queue = [it for it in self.bot_queue if it["agent_id"] != agent_id]
        entry = self.bots.get(agent_id)
        if entry is None:
            return bool(dropped)
        entry["agent"].cancel()
        self.deny_all(entry["run"].id)
        return True

    @property
    def ws(self) -> WebSocket | None:  # kept for older callers/tests
        return next(iter(self.clients), None)

    def busy_now(self) -> bool:
        return bool(self.agent.busy or self.mcp_client or (self.task is not None and not self.task.done()))

    def _wait_limit(self, run=None) -> float | None:
        """Nobody may be at the PC for a bot's work or a task from the phone: an unanswered approval is refused
        after a while instead of holding it forever."""
        if not (run is not None and run.is_bot) and self.source == "chat":
            return None
        from .scheduler import UNATTENDED_MINUTES
        return UNATTENDED_MINUTES * 60.0

    def _tagged(self, request: dict) -> tuple[dict, object]:
        """Every approval or question gets its own id, and says which run (and bot) it's for."""
        from . import runctx
        run = runctx.current()
        return {**request, "call_id": request.get("id"), "id": uuid.uuid4().hex, "run": run.id, "bot": run.bot,
                "source": run.source if run.is_bot else self.source}, run

    async def _tell(self, method: str, *args) -> None:
        for listener in list(self.listeners):
            try:
                await getattr(listener, method)(*args)
            except Exception:  # noqa: BLE001 - a listener (e.g. WhatsApp) failing never stops the task
                pass

    async def submit(self, text: str, source: str = "chat", label: str = "") -> str:
        """Start a task now, or queue it while Karya is busy. Returns 'started' or 'queued'."""
        label = label or source
        if self.busy_now():
            self.queue.append({"text": text, "source": source, "label": label})
            await self.send({"type": "note", "text": f"Queued a task from {label}: {text[:150]}"})
            return "queued"
        await self.start_run(text, source, label)
        return "started"

    async def next_queued(self) -> bool:
        if self.queue and not self.busy_now():
            item = self.queue.pop(0)
            await self.start_run(item["text"], item["source"], item["label"])
            return True
        return False

    async def send(self, event: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                self.clients.discard(ws)

    async def confirm(self, request: dict) -> bool:
        # Tool-call ids can repeat across turns (or be empty) with some providers, so every approval
        # request gets its own id; the card and the answer are matched on that.
        request, run = self._tagged(request)
        future = asyncio.get_running_loop().create_future()
        self.pending[request["id"]] = (future, request)
        await self.send({"type": "confirm", **request})
        await self._tell("on_confirm", request)
        from .ext_link import link
        who = f"{run.bot}: " if run.is_bot else ""
        link.notify("attention", {"on": True, "text": (who + str(request.get("summary") or "approval needed"))[:150]})
        approved = False
        try:
            limit = self._wait_limit(run)
            try:
                approved = await (asyncio.wait_for(future, limit) if limit else future)
            except asyncio.TimeoutError:
                approved = False
                await self.send({"type": "note", "text": f"Nobody answered for {int(limit // 60)} minutes, so Karya "
                                                         "didn't do it."})
            await self.send({"type": "confirm_done", "id": request["id"], "approved": bool(approved)})
            return approved
        finally:
            self.pending.pop(request["id"], None)
            await self._tell("on_confirm_done", request["id"], bool(approved))
            if not self.pending and not self.asks:
                link.notify("attention", {"on": False})

    def answer(self, request_id: str, approved: bool) -> None:
        entry = self.pending.get(request_id)
        if entry and not entry[0].done():
            entry[0].set_result(bool(approved))

    def deny_all(self, run_id: str = "main") -> None:
        """Refuse the open approvals and questions of one run (the chat's by default; bots keep theirs)."""
        for future, request in list(self.pending.values()):
            if request.get("run", "main") == run_id and not future.done():
                future.set_result(False)
        for future, request in list(self.asks.values()):
            if request.get("run", "main") == run_id and not future.done():
                future.set_result(None)

    async def ask(self, request: dict) -> dict | None:
        """Secure form (e.g. a login). The answer goes to the agent's vault code, never into the chat history."""
        request, run = self._tagged(request)
        future = asyncio.get_running_loop().create_future()
        self.asks[request["id"]] = (future, request)
        await self.send({"type": "ask", **request})
        await self._tell("on_ask", request)
        from .ext_link import link
        link.notify("attention", {"on": True, "text": f"{run.bot or 'Karya'} is asking you something in Karya's chat"})
        answer = None
        try:
            limit = self._wait_limit(run)
            try:
                answer = await (asyncio.wait_for(future, limit) if limit else future)
            except asyncio.TimeoutError:
                answer = None
            await self.send({"type": "ask_done", "id": request["id"], "answered": bool(answer)})
            return answer
        finally:
            self.asks.pop(request["id"], None)
            await self._tell("on_ask_done", request["id"], bool(answer))
            if not self.pending and not self.asks:
                link.notify("attention", {"on": False})

    def answer_ask(self, request_id: str, data: dict | None) -> None:
        entry = self.asks.get(request_id)
        if entry and not entry[0].done():
            entry[0].set_result(data)

    async def start_run(self, text: str, source: str = "chat", label: str = "") -> None:
        self.source, self.label = source, label or ("the chat" if source == "chat" else source)

        async def runner():
            await self.send({"type": "busy", "value": True})
            final = ""
            try:
                if source != "chat":  # the chat page shows what came from the phone or an agent
                    await self.send({"type": "user", "text": text, "via": self.label})
                final = await self.agent.run(text, self.send, self.confirm, self.ask) or ""
            except Exception as exc:  # keep the server alive whatever happens
                final = f"Internal error: {type(exc).__name__}: {exc}"
                await self.send({"type": "error", "text": final})
            finally:
                await self.send({"type": "busy", "value": False})
                self.source, self.label = "chat", "the chat"
                await self._tell("on_done", source, text, final)
                if self.queue and not self.mcp_client:
                    item = self.queue.pop(0)
                    await self.start_run(item["text"], item["source"], item["label"])
        self.task = asyncio.create_task(runner())


def create_app(agent: Agent | None = None, token: str | None = None, port: int | None = None,
               extra_hosts: set[str] | None = None, background: bool = False) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """The real app (not tests) runs background agents and takes tasks from WhatsApp."""
        if background:
            from . import phone, scheduler
            hub: Hub = app.state.hub
            scheduler.HUB, scheduler.LOOP = hub, asyncio.get_running_loop()
            channel = phone.start(hub, asyncio.get_running_loop())
            hub.listeners += [scheduler.Hooks(), channel]
            app.state.scheduler_task = asyncio.create_task(scheduler.loop(hub))
            from . import awake
            awake.start(hub)
        yield
        if background:
            app.state.scheduler_task.cancel()
            from . import phone
            if phone.CHANNEL is not None and phone.CHANNEL.bridge is not None:
                phone.CHANNEL.bridge.stop()

    app = FastAPI(title="Karya", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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
        from . import phone, scheduler
        return {**settings.status(), "tools": len(TOOLS), "auto_mode": hub.agent.auto_mode, "busy": hub.agent.busy,
                "running": hub.label if hub.busy_now() else None, "queued": len(hub.queue),
                "whatsapp": phone.CHANNEL.status() if phone.CHANNEL else {"state": "off", "detail": ""},
                "agents": [scheduler._row(a) for a in scheduler.load()], "bots_running": hub.bots_running(),
                "keep_awake": settings.keep_awake,
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
        hub.set_llm(LLMClient(settings.providers))  # new keys take effect for the next message
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

    # ---------------------------------------------------------------- Setup > Connect your AI
    def refresh_ai() -> None:
        from .llm import LLMClient
        app.state.hub.set_llm(LLMClient(settings.providers))  # the chat and the bots use it from the next step on

    async def check(name: str) -> list[dict]:
        return await asyncio.to_thread(_check_providers, app.state.hub.agent.llm, {name})

    @app.get("/api/ai")
    async def ai_overview(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import connect
        return await asyncio.to_thread(connect.overview)

    @app.post("/api/ai")
    async def ai_action(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import connect
        try:
            body = dict(await request.json())
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        action, name = str(body.get("action", "")), str(body.get("provider", "")).lower()
        try:
            if action == "key":
                out = await asyncio.to_thread(connect.save_key, str(body.get("key", "")), str(body.get("service", "")))
                if out.get("saved"):
                    refresh_ai()
                    out["checks"] = await check(out["saved"])
            elif action == "models":
                out = {"provider": name, "models": await asyncio.to_thread(connect.models_for, name, bool(body.get("fresh")))}
            elif action == "model":
                out = connect.set_model(name, str(body.get("model", "")))
                refresh_ai()
                out["checks"] = await check(name)
            elif action == "first":
                out = connect.use_first(name)
                refresh_ai()
            elif action == "remove":
                out = await asyncio.to_thread(connect.remove, name)
                refresh_ai()
            elif action == "login":
                out = await asyncio.to_thread(connect.start_login, name, str(body.get("method", "")))
            elif action == "login_status":
                out = connect.login_progress(name)
                if out.get("state") == "done" and not body.get("checked"):
                    refresh_ai()
                    out["checks"] = await check(name)
            elif action == "login_cancel":
                connect.cancel_login(name)
                out = {"state": "idle"}
            elif action == "use_codex":
                out = connect.use_codex()
                refresh_ai()
                out["checks"] = await check("codex")
            else:
                return JSONResponse({"error": f"unknown action {action!r}"}, status_code=400)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {**out, "overview": connect.overview(include_logins=False)}

    @app.get("/api/mcp/apps")
    async def mcp_apps_list(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import mcp_apps
        return {"apps": await asyncio.to_thread(mcp_apps.overview)}

    @app.post("/api/mcp/apps")
    async def mcp_apps_connect(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import mcp_apps
        try:
            body = dict(await request.json())
            return await asyncio.to_thread(mcp_apps.connect, str(body.get("app", "")))
        except (ValueError, TypeError, OSError, subprocess.SubprocessError) as exc:
            return JSONResponse({"error": str(exc)[:300]}, status_code=400)

    # ---------------------------------------------------------------- Setup > Phone and background agents
    @app.post("/api/phone")
    async def phone_action(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import phone
        if phone.CHANNEL is None:
            return JSONResponse({"error": "WhatsApp needs Karya's app (start.bat)."}, status_code=400)
        try:
            body = dict(await request.json())
        except (ValueError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        if body.get("action") == "disconnect":
            return await asyncio.to_thread(phone.CHANNEL.disable)
        number = re.sub(r"\D", "", str(body.get("number", "")))
        if number:
            if not 10 <= len(number) <= 15:
                return JSONResponse({"error": "Type your WhatsApp number with its country code, e.g. +91 98xxxxxxxx."},
                                    status_code=400)
            from .memory import memory_store
            data = memory_store.load()
            data.setdefault("profile", {})["whatsapp"] = "+" + number
            memory_store.save(data)
        return await asyncio.to_thread(phone.CHANNEL.enable, 20.0)

    @app.post("/api/agents")
    async def agents_action(request: Request, token: str = ""):
        if not allowed(request, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        from . import scheduler
        hub: Hub = app.state.hub
        note = ""
        try:
            body = dict(await request.json())
            action, key = str(body.get("action", "")), str(body.get("agent", ""))
            found = scheduler.find(key) if action != "create" else None
            if action != "create" and found is None:
                raise ValueError(f"no bot {key!r}")
            if action == "create":
                when = str(body.get("when", "on_demand"))
                created = scheduler.create(str(body.get("name", "")), str(body.get("task", "")),
                                           every_minutes=int(float(body.get("hours") or 0) * 60) or None
                                           if when == "every" else None,
                                           daily_at=[str(body.get("time") or "09:00")] if when == "daily" else None,
                                           weekdays=["weekdays"] if body.get("weekdays_only") else None)
                note = f"Made {created['name']}. Give it a task below, or write \"@{created['name']} ...\" in the chat."
            elif action == "delete":
                hub.stop_bot(found["id"])
                scheduler.delete(found["id"])
            elif action == "stop":
                note = "Stopped." if hub.stop_bot(found["id"]) else "It wasn't working on anything."
            elif action in ("assign", "run"):
                task = str(body.get("task", "")).strip()[:4000] if action == "assign" else None
                if action == "assign" and not task:
                    raise ValueError("type the task first")
                state = await hub.assign(found["id"], task, source="assigned", user_words=task)
                note = f"{found['name']} {'started' if state == 'started' else 'has it queued'}."
            elif action in ("pause", "resume"):
                scheduler.update(found["id"], enabled=action == "resume")
            else:
                return JSONResponse({"error": f"unknown action {action!r}"}, status_code=400)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"agents": [scheduler._row(a) for a in scheduler.load()], "note": note}

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
                                   "busy": hub.agent.busy, "auto_mode": hub.agent.auto_mode, "keep_going": settings.keep_going,
                                   "bots": hub.bots_running()})
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
                    from . import scheduler
                    target = scheduler.addressed(text)
                    if target:                      # "@Maya find PM jobs": straight to that bot, the chat stays free
                        bot, task = target
                        state = await hub.assign(bot["id"], task[:4000], source="assigned", user_words=task[:4000])
                        await hub.send({"type": "assistant", "text": (
                            f"{bot['name']} is on it. It works in the background and reports here (and on WhatsApp)."
                            if state == "started" else
                            f"{bot['name']} has it queued: it finishes its current work first.")})
                        await websocket.send_json({"type": "busy", "value": bool(hub.agent.busy)})
                        continue
                    if hub.agent.busy:
                        running = "the last request" if hub.source == "chat" else f"a task from {hub.label}"
                        await hub.send({"type": "error", "text": f"I'm still working on {running}. Press Stop first."})
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
                elif kind == "stop_bot":
                    hub.stop_bot(str(data.get("agent", "")))
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
    app = create_app(token=token, port=port, background=True)
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
