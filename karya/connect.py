"""Setup > "Connect your AI": the simple way in. Paste any key (Karya works out which service it's from), or sign in
with a subscription you already pay for (Kiro, or a ChatGPT plan through the Codex CLI), then pick the model and which
AI goes first. Everything is saved in .env on this PC; keys are never shown back."""
from __future__ import annotations

import re
import time

from .config import CODEX_TITLE, PRESET_BY_NAME, PRESETS, _env, detect_key, provider_order, settings, update_env

_NOT_CHAT = re.compile(r"embed|tts|whisper|dall-?e|moderation|rerank|guard|audio|image|transcri|realtime|search|"
                       r"speech|vision-preview|davinci|babbage|\bocr\b|sora|veo|imagen|lyria", re.I)
_MODEL_OK = re.compile(r"^[\w.:/@+\-]{1,120}$")
_MODELS: dict[str, tuple[float, list[dict]]] = {}


def _title(name: str) -> str:
    if name == "codex":
        return CODEX_TITLE
    if name == "ollama":
        return "Offline model (Ollama)"
    if name == "custom":
        return "Your own OpenAI-compatible API"
    preset = PRESET_BY_NAME.get(name)
    return preset.title if preset else name


def overview(include_logins: bool = True) -> dict:
    """What the Connect card shows: the AIs Karya uses (in order), and the subscriptions it can sign in to."""
    from . import codex_bridge, kiro_bridge
    rows = []
    for p in settings.providers:
        how = "offline" if p.name == "ollama" else "sign-in" if p.api_key == "login" else "key"
        if p.name == "kiro" and p.api_key != "login":
            how = "Kiro key"
        rows.append({"name": p.name, "title": _title(p.name), "model": p.model or "auto", "how": how})
    out = {"connected": rows,
           "services": [{"name": p.name, "title": p.title, "signup": p.signup, "free": p.free} for p in PRESETS]}
    if include_logins:
        out["kiro"] = {**kiro_bridge.login_status(), "key": bool(_env("KIRO_API_KEY")), "progress": kiro_bridge.login_progress()}
        out["codex"] = {**codex_bridge.login_status(), "enabled": settings_has("codex"),
                        "progress": codex_bridge.login_progress()}
    return out


def settings_has(name: str) -> bool:
    return any(p.name == name for p in settings.providers)


def _order_env(**change) -> dict:
    """LLM_PROVIDERS only changes when the user has a custom order, or asked to move one first."""
    if change.get("first") or _env("LLM_PROVIDERS"):
        return {"LLM_PROVIDERS": provider_order(**change)}
    return {}


def save_key(key: str, service: str = "") -> dict:
    """Store a pasted key under the right service. Returns {'saved': name} or {'choose': [names]} when the key's
    shape fits several services (the key is never sent anywhere to find out)."""
    key = (key or "").strip().strip('"').strip("'")
    if not key or len(key) > 400 or re.search(r"\s", key):
        raise ValueError("That doesn't look like an API key (paste the whole key, without spaces).")
    service = (service or "").strip().lower()
    if service and service not in PRESET_BY_NAME:
        raise ValueError(f"unknown service {service!r}")
    names = [service] if service else detect_key(key)
    if len(names) != 1:
        return {"choose": names or [p.name for p in PRESETS if p.name != "kiro"]}
    name = names[0]
    update_env({PRESET_BY_NAME[name].key_env: key, **_order_env(add=name)})
    return {"saved": name, "title": _title(name)}


def models_for(name: str, fresh: bool = False) -> list[dict]:
    """[{'id', 'name', 'description'}] the service offers to this key or sign-in (cached for an hour)."""
    cached = _MODELS.get(name)
    if cached and not fresh and time.time() - cached[0] < 3600:
        return cached[1]
    provider = next((p for p in settings.providers if p.name == name), None)
    if provider is None:
        return []
    out: list[dict] = []
    if name == "codex":
        from . import codex_bridge
        out = codex_bridge.models()
    elif name == "kiro":
        from . import kiro_bridge
        try:
            out = kiro_bridge.bridge_for(provider.api_key, "bg").models()
        except kiro_bridge.KiroError:
            out = []
    else:
        from .llm import LLMClient
        try:
            ids = sorted({m.id for m in LLMClient([provider])._client(provider).models.list().data})
        except Exception:  # noqa: BLE001 - a service without a model list: the picker allows typing a name
            ids = []
        for mid in ids:
            short = mid.removeprefix("models/")
            if not _NOT_CHAT.search(short):
                out.append({"id": short, "name": short, "description": ""})
        out = out[:400]
    _MODELS[name] = (time.time(), out)
    return out


def set_model(name: str, model: str) -> dict:
    model = (model or "").strip()
    if not settings_has(name):
        raise ValueError(f"{_title(name)} isn't connected")
    if model.lower() != "auto" and not _MODEL_OK.match(model):
        raise ValueError("That isn't a model name.")
    key = "CUSTOM_MODEL" if name == "custom" else f"{name.upper()}_MODEL"
    update_env({key: model})
    return {"provider": name, "model": model}


def use_first(name: str) -> dict:
    if not settings_has(name):
        raise ValueError(f"{_title(name)} isn't connected")
    update_env({"LLM_PROVIDERS": provider_order(first=name)})
    return {"first": name}


def remove(name: str) -> dict:
    if name == "ollama":
        raise ValueError("The offline model is Karya's last resort; it can't be removed here.")
    changes: dict = {}
    if name == "kiro":
        from . import kiro_bridge
        changes.update(KIRO_API_KEY="", KIRO_LOGIN="false")
        kiro_bridge.logout()
    elif name == "codex":
        changes["CODEX_ENABLED"] = "false"       # the Codex CLI stays signed in for your own use
    elif name == "custom":
        changes.update(CUSTOM_BASE_URL="", CUSTOM_API_KEY="", CUSTOM_MODEL="")
    elif name in PRESET_BY_NAME:
        changes[PRESET_BY_NAME[name].key_env] = ""
    else:
        raise ValueError(f"unknown AI {name!r}")
    update_env({**changes, **_order_env(drop=name)})
    _MODELS.pop(name, None)
    return {"removed": name}


def start_login(name: str, method: str = "") -> dict:
    from . import codex_bridge, kiro_bridge
    if name == "kiro":
        return kiro_bridge.start_login(method or "google")
    if name == "codex":
        return codex_bridge.start_login(device=method == "device")
    raise ValueError("Sign-in works for Kiro and ChatGPT (Codex).")


def login_progress(name: str) -> dict:
    """While signing in: the code or address to show. When done, the subscription becomes Karya's first AI."""
    from . import codex_bridge, kiro_bridge
    if name == "kiro":
        progress = kiro_bridge.login_progress()
        if progress.get("state") == "done" and _env("KIRO_LOGIN") != "true":
            update_env({"KIRO_LOGIN": "true", **_order_env(first="kiro")})
    elif name == "codex":
        progress = codex_bridge.login_progress()
        if progress.get("state") == "done" and _env("CODEX_ENABLED") != "true":
            update_env({"CODEX_ENABLED": "true", **_order_env(first="codex")})
    else:
        raise ValueError("Sign-in works for Kiro and ChatGPT (Codex).")
    return progress


def use_codex() -> dict:
    """The Codex CLI is already signed in on this PC: just turn it on as Karya's first AI."""
    from . import codex_bridge
    status = codex_bridge.login_status(fresh=True)
    if not status.get("signed_in"):
        raise ValueError("Codex isn't signed in yet: use Sign in with ChatGPT.")
    update_env({"CODEX_ENABLED": "true", **_order_env(first="codex")})
    return {"enabled": "codex"}


def cancel_login(name: str) -> None:
    from . import codex_bridge, kiro_bridge
    (kiro_bridge if name == "kiro" else codex_bridge).cancel_login()
