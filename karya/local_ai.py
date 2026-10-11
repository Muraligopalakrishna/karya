"""Setup > Connect your AI > "Free, on this PC (Ollama)".

Karya's offline AI is a small open model (Qwen 3.5, 4B) that runs on the user's PC through Ollama: free and private,
no account, slow without a graphics card. Ollama alone does nothing until the model is downloaded (about 3 GB), so
this module tells the truth about each step (Ollama installed? running? model there?) and does the download with one
click: `ollama pull qwen3.5:4b`, then `ollama create karya-qwen -f Modelfile` (Karya's settings for it)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

import requests

from .config import ROOT, _env

BASE = "http://localhost:11434"
BASE_MODEL = "qwen3.5:4b"
KARYA_MODEL = "karya-qwen"
INSTALL_URL = "https://ollama.com/download"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PULL: dict = {"state": "idle", "percent": 0, "detail": "", "error": ""}


def _api() -> str:
    url = _env("OLLAMA_BASE_URL", BASE + "/v1")
    return re.sub(r"/v1/?$", "", url)


def find_cli() -> str | None:
    return shutil.which("ollama") or shutil.which("ollama.exe")


def installed_models() -> list[dict]:
    """[{'name', 'gb'}] from the running Ollama ([] when it isn't running)."""
    try:
        data = requests.get(_api() + "/api/tags", timeout=4).json()
    except (requests.RequestException, ValueError):
        return []
    return [{"name": m.get("name", ""), "gb": round((m.get("size") or 0) / 1e9, 1)} for m in data.get("models") or []]


def _has(names: list[str], model: str) -> bool:
    return any(n == model or n == model + ":latest" or n.split(":")[0] == model for n in names)


_STATUS: dict = {"at": 0.0, "value": None}


def status(cached: bool = False) -> dict:
    """installed / running / ready (Karya's model there) / which models / a download in progress.
    cached: reuse a result up to 15 s old (the page asks for Karya's status often)."""
    if cached and _STATUS["value"] is not None and time.time() - _STATUS["at"] < 15 and PULL["state"] != "downloading":
        return _STATUS["value"]
    running = True
    try:
        requests.get(_api() + "/api/version", timeout=3)
    except requests.RequestException:
        running = False
    names = [m["name"] for m in installed_models()] if running else []
    want = _env("OLLAMA_MODEL", KARYA_MODEL)
    out = {"installed": bool(find_cli()) or running, "running": running,
           "ready": running and (_has(names, want) or _has(names, BASE_MODEL)),
           "model": want, "models": names[:20], "install": INSTALL_URL}
    if PULL["state"] not in ("idle",):
        out["download"] = {k: PULL[k] for k in ("state", "percent", "detail", "error") if PULL.get(k) not in ("", None)}
    _STATUS.update(at=time.time(), value=out)
    return out


def models() -> list[dict]:
    return [{"id": m["name"].removesuffix(":latest"), "name": m["name"].removesuffix(":latest"),
             "description": f"{m['gb']} GB on this PC"} for m in installed_models()]


def _start_server() -> None:
    cli = find_cli()
    if cli:
        subprocess.Popen([cli, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW)
        for _ in range(20):
            time.sleep(0.5)
            try:
                requests.get(_api() + "/api/version", timeout=2)
                return
            except requests.RequestException:
                continue


def _pull(model: str) -> None:
    """Stream Ollama's pull progress into PULL."""
    with requests.post(_api() + "/api/pull", json={"model": model, "stream": True}, stream=True, timeout=(10, 600)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("error"):
                raise RuntimeError(event["error"])
            total, done = event.get("total") or 0, event.get("completed") or 0
            if total:
                PULL["percent"] = int(done * 100 / total)
            PULL["detail"] = str(event.get("status") or "")[:80]


def download() -> dict:
    """One click: start Ollama if needed, download the model, add Karya's settings. Runs in the background."""
    if PULL["state"] == "downloading":
        return status()
    if not find_cli() and not status()["running"]:
        raise ValueError(f"Install Ollama first (free): {INSTALL_URL}")
    PULL.update(state="downloading", percent=0, detail="starting", error="")

    def work():
        try:
            if not status()["running"]:
                _start_server()
            _pull(BASE_MODEL)
            PULL.update(detail="adding Karya's settings", percent=100)
            cli = find_cli()
            modelfile = ROOT / "Modelfile"
            if cli and modelfile.exists():
                subprocess.run([cli, "create", KARYA_MODEL, "-f", str(modelfile)], capture_output=True, timeout=300,
                               creationflags=_NO_WINDOW)
            from .config import update_env
            names = [m["name"] for m in installed_models()]
            update_env({"OLLAMA_ENABLED": "true",
                        "OLLAMA_MODEL": KARYA_MODEL if _has(names, KARYA_MODEL) else BASE_MODEL})
            PULL.update(state="done", detail="ready")
        except Exception as exc:  # noqa: BLE001 - shown in Setup
            PULL.update(state="failed", error=str(exc)[:200])
    threading.Thread(target=work, daemon=True, name="ollama-pull").start()
    time.sleep(1.0)
    return status()
