"""OpenAI-compatible chat client that tries each configured provider (and each of its models) in order."""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import threading
import time
import uuid
from typing import Callable

import openai
import requests
from openai import OpenAI

from .config import DATA_DIR, Provider

LIMITS_FILE = DATA_DIR / "cache" / "limits.json"   # learned per-model limits (tokens per minute, context window)
MODELS_FILE = DATA_DIR / "cache" / "models.json"   # each provider's model list (refreshed daily)
ACTIVE = None   # the agent's LLMClient, so a tool can make one focused AI request (import/tailor a resume)
# Which Kiro CLI process this thread's requests use (background work gets its own, so it runs in parallel).
LANE = threading.local()


def _is_too_large(exc: Exception) -> bool:
    low = str(exc).lower()
    return ("request too large" in low or "too many tokens" in low or "reduce your message size" in low
            or ("requested" in low and "limit" in low and "tokens per minute" in low and "used" not in low))

GEMINI_DUMMY_SIGNATURE = "skip_thought_signature_validator"
MAX_RATE_WAIT = 65.0          # longest single wait for a rate-limited cloud model (per-minute limits reset within 60s)
MAX_TOTAL_WAIT = 180.0        # per step: keep waiting for the cloud this long before using the slow offline model
TEXT_CHARS_PER_TOKEN = 3.8    # measured on Groq: ~4.0-4.4 for chat/tool text (kept a little conservative)
TOOL_CHARS_PER_TOKEN = 6.5    # measured on Groq: ~7.1 for JSON tool schemas
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_TRY_AGAIN_RE = re.compile(r"try again in\s+([\d.hms]+)", re.I)
_LIMIT_KIND_RE = re.compile(r"\bon ([a-z ]+?) \((\w+)\)", re.I)

Notify = Callable[[str], None]


class LLMError(RuntimeError):
    pass


# Some models (gpt-oss) write U+2011 non-breaking hyphens and U+202F narrow spaces ("entry‑level", "4‑6 LPA").
# They break URLs/commands and look odd in emails and posts, so they are turned into plain characters.
_ODD_CHARS = {"\u2011": "-", "\u2010": "-", "\u202f": " ", "\u00a0": " ", "\u2007": " ", "\u2009": " ",
              "\u200a": " ", "\u200b": ""}
_ODD_RE = re.compile("|".join(map(re.escape, _ODD_CHARS)))
_ODD_ESC_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\u(2011|2010|202[fF]|00[aA]0|2007|2009|200[aAbB])")


def clean_text(text: str) -> str:
    if not text:
        return text
    text = _ODD_RE.sub(lambda m: _ODD_CHARS[m.group(0)], text)
    return _ODD_ESC_RE.sub(lambda m: m.group(1) + _ODD_CHARS[chr(int(m.group(2), 16))], text)


def prepare_messages(provider: Provider, messages: list[dict]) -> list[dict]:
    """Strip internal keys and keep only the fields each provider accepts (Groq rejects unknown ones)."""
    out: list[dict] = []
    for msg in messages:
        m = {k: copy.deepcopy(v) for k, v in msg.items() if not k.startswith("_")}
        if m.get("role") == "assistant" and m.get("tool_calls"):
            clean_calls = []
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                args = fn.get("arguments")
                call = {"id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                        "function": {"name": fn.get("name", ""),
                                     "arguments": args if isinstance(args, str) else json.dumps(args or {})}}
                if provider.name == "gemini" and c.get("extra_content"):
                    call["extra_content"] = c["extra_content"]
                clean_calls.append(call)
            if provider.name == "gemini" and not any(
                    (c.get("extra_content") or {}).get("google", {}).get("thought_signature") for c in clean_calls):
                # history written by another provider: tell Gemini to skip signature validation
                clean_calls[0]["extra_content"] = {"google": {"thought_signature": GEMINI_DUMMY_SIGNATURE}}
            m["tool_calls"] = clean_calls
            if not m.get("content"):
                m["content"] = None
        if m.get("role") == "tool" and not m.get("content"):
            m["content"] = "(no output)"
        out.append(m)
    return out


def _message_to_dict(msg) -> dict:
    content = clean_text(_THINK_RE.sub("", msg.content or "").strip())
    result: dict = {"role": "assistant", "content": content}
    calls = []
    for tc in msg.tool_calls or []:
        data = tc.model_dump(exclude_none=True)  # keeps provider extras such as Gemini thought signatures
        if not data.get("id"):
            data["id"] = f"call_{uuid.uuid4().hex[:12]}"
        fn = data.get("function") or {}
        if isinstance(fn.get("arguments"), str):
            fn["arguments"] = clean_text(fn["arguments"])
        calls.append(data)
    if not calls and "<tool_call>" in content:  # some local models print tool calls as text
        for match in _TOOLCALL_RE.finditer(content):
            try:
                obj = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                          "function": {"name": obj.get("name", ""),
                                       "arguments": clean_text(json.dumps(obj.get("arguments") or obj.get("parameters") or {}))}})
        if calls:
            result["content"] = _TOOLCALL_RE.sub("", content).strip()
    if calls:
        result["tool_calls"] = calls
    return result


def _parse_duration(text: str) -> float:
    units = {"ms": 0.001, "h": 3600.0, "m": 60.0, "s": 1.0}
    return sum(float(v) * units[u] for v, u in re.findall(r"([\d.]+)(ms|h|m|s)", text))


def _retry_after(exc: Exception, default: float) -> float:
    try:
        value = exc.response.headers.get("retry-after")  # type: ignore[attr-defined]
        if value:
            return max(1.0, float(value))
    except Exception:
        pass
    match = _TRY_AGAIN_RE.search(str(exc))
    if match:
        seconds = _parse_duration(match.group(1))
        if seconds > 0:
            return max(1.0, seconds)
    return default


def _limit_kind(exc: Exception) -> str:
    match = _LIMIT_KIND_RE.search(str(exc))
    return match.group(2).upper() if match else "limit"


_NOT_CHAT = re.compile(r"embed|tts|whisper|transcri|audio|realtime|image|dall-e|moderation|search|guard|rerank|"
                       r"vision-preview|computer-use|-instruct-\d{4}|davinci|babbage", re.I)


def choose_model(ids: list[str], prefer: tuple[str, ...]) -> str | None:
    """Best chat model from a provider's model list: first matching preference pattern, newest version first."""
    def version(model_id: str) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", model_id)[:4])

    candidates = [i for i in ids if not _NOT_CHAT.search(i)]
    for pattern in prefer:
        found = [i for i in candidates if re.search(pattern, i)]
        if found:
            found.sort(key=lambda i: (version(i), -len(i)), reverse=True)
            return found[0]
    return None


def _json_load(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _json_save(path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


_LIMIT_NUM_RE = re.compile(r"\bLimit\s*[:=]?\s*(\d{3,9})", re.I)
_CTX_NUM_RE = re.compile(r"(?:maximum context length|context (?:length|window)|max(?:imum)? (?:input )?tokens?)\D{0,40}?(\d{4,9})", re.I)


class LLMClient:
    def __init__(self, providers: list[Provider]):
        self.providers = providers
        self._clients: dict[tuple, OpenAI] = {}
        self.cooldown: dict[tuple[str, str], float] = {}   # (provider, model) -> time it can be used again
        self.last_used: Provider | None = None
        self.limits: dict[str, dict] = _json_load(LIMITS_FILE)   # "provider:model" -> {"tpm", "ctx", "max"}

    def _client(self, p: Provider) -> OpenAI:
        key = (p.name, p.base_url, p.api_key)
        if key not in self._clients:
            self._clients[key] = OpenAI(api_key=p.api_key or "none", base_url=p.base_url,
                                        timeout=p.timeout, max_retries=0)
        return self._clients[key]

    # ------------------------------------------------------------ learned limits
    def _limit(self, p: Provider, model: str) -> dict:
        return self.limits.setdefault(f"{p.name}:{model}", {})

    def budget_tokens(self, p: Provider, model: str) -> int | None:
        """Per-request input budget: what this provider/model/plan actually allows (learned), else a safe start."""
        lim = self.limits.get(f"{p.name}:{model}", {})
        caps = []
        if lim.get("ctx"):
            caps.append(int(lim["ctx"] * 0.85))
        if lim.get("tpm"):
            caps.append(int(lim["tpm"] * 0.7))
        if lim.get("max"):
            caps.append(int(lim["max"]))
        if not lim.get("tpm") and not lim.get("max") and p.max_input_tokens:
            caps.append(p.max_input_tokens)
        return min(caps) if caps else None

    def _learn(self, p: Provider, model: str, **values) -> None:
        lim = self._limit(p, model)
        changed = False
        for key, value in values.items():
            if value and lim.get(key) != value:
                lim[key] = int(value)
                changed = True
        if changed:
            lim["updated"] = int(time.time())
            _json_save(LIMITS_FILE, self.limits)

    def _learn_headers(self, p: Provider, model: str, headers) -> None:
        try:
            tpm = int(headers.get("x-ratelimit-limit-tokens") or 0)
        except (TypeError, ValueError):
            tpm = 0
        if tpm > 0:
            self._learn(p, model, tpm=tpm)
            lim = self._limit(p, model)
            if lim.get("max") and lim["max"] < tpm * 0.5:  # plan was upgraded: forget an old, smaller cap
                lim.pop("max", None)

    # ------------------------------------------------------------ model choice
    def _resolve(self, p: Provider) -> str | None:
        if p.model:
            return p.model
        cache = _json_load(MODELS_FILE)
        key = f"{p.name}|{p.base_url}"
        entry = cache.get(key) or {}
        if time.time() - entry.get("time", 0) > 24 * 3600:
            try:
                data = self._client(p).models.list().data
                entry = {"time": time.time(), "ids": [m.id for m in data],
                         "ctx": {m.id: (getattr(m, "context_window", None) or getattr(m, "context_length", None)
                                        or (m.model_extra or {}).get("context_window") or (m.model_extra or {}).get("context_length"))
                                 for m in data}}
                cache[key] = entry
                _json_save(MODELS_FILE, cache)
            except Exception:
                entry = entry or {"ids": [], "ctx": {}}
        pick = choose_model(entry.get("ids") or [], p.prefer) or (p.fallback_models[0] if p.fallback_models else None)
        if pick:
            p.model = pick
            ctx = (entry.get("ctx") or {}).get(pick)
            if ctx:
                self._learn(p, pick, ctx=int(ctx))
        return p.model or None

    def _models(self, p: Provider) -> list[str]:
        first = self._resolve(p)
        if not first:
            return []
        return [first, *[m for m in p.fallback_models if m != first]]

    # Per-request state lives per thread: background requests (resume tailoring) run at the same time as Karya's
    # steps and must never pick up the agent's system prompt.
    def _tls(self) -> threading.local:
        tls = self.__dict__.get("_tls_obj")
        if tls is None:
            tls = self.__dict__.setdefault("_tls_obj", threading.local())
        return tls

    @property
    def _system_for(self):
        return getattr(self._tls(), "system_for", None)

    @_system_for.setter
    def _system_for(self, value) -> None:
        self._tls().system_for = value

    def chat(self, messages: list[dict], tools: list[dict] | Callable[[Provider], list[dict]] | None = None,
             trim: Callable[[list[dict], int], list[dict]] | None = None, notify: Notify | None = None,
             cancel: threading.Event | None = None, system_for: Callable[[int | None], str] | None = None
             ) -> tuple[dict, Provider]:
        self._system_for = system_for
        if not self.providers:
            raise LLMError("No AI provider set up. Open Setup and add any API key (OpenAI, Claude, Gemini, Groq, "
                           "OpenRouter, DeepSeek, Mistral, xAI...), free or paid, or install Ollama for offline use.")
        errors: list[str] = []
        cloud = [p for p in self.providers if not p.slow]
        slow = [p for p in self.providers if p.slow]
        deadline = time.time() + MAX_TOTAL_WAIT
        # 1) cloud models, rotating between them; short free-plan limits are waited out (much faster than the offline model)
        while cloud:
            waits: list[float] = []
            for p in cloud:
                for model in self._models(p):
                    self._check_cancel(cancel)
                    ready_at = self.cooldown.get((p.name, model), 0.0)
                    if ready_at == float("inf"):
                        continue
                    if ready_at > time.time():
                        waits.append(ready_at)
                        continue
                    kind, value = self._try(p, model, messages, tools, trim, errors)
                    if kind == "ok":
                        return self._used(p, model, value)
                    if kind == "rate":
                        self.cooldown[(p.name, model)] = time.time() + float(value)
                        waits.append(time.time() + float(value))
                    elif kind == "gone":
                        self.cooldown[(p.name, model)] = float("inf")
                    elif kind == "auth":
                        for m in self._models(p):
                            self.cooldown[(p.name, m)] = float("inf")
                        break
                    elif kind == "next_provider":
                        break
            if not waits:
                break
            delay = min(waits) - time.time()
            if delay > MAX_RATE_WAIT or time.time() + delay > deadline:
                break
            if delay > 0:
                if notify:
                    notify(f"AI rate limit reached - waiting {int(delay) + 1}s...")
                self._sleep(delay + 0.5, cancel)
        # 2) last resort: slow offline model(s)
        for p in slow:
            if notify:
                notify("Using the offline AI on this PC (slow, can take a few minutes)...")
            for model in self._models(p):
                self._check_cancel(cancel)
                if self.cooldown.get((p.name, model), 0.0) == float("inf"):
                    continue
                kind, value = self._try(p, model, messages, tools, trim, errors)
                if kind == "ok":
                    return self._used(p, model, value)
                if kind == "gone":
                    self.cooldown[(p.name, model)] = float("inf")
                elif kind in ("next_provider", "auth"):
                    break
        raise LLMError("All AI providers failed -> " + " | ".join(errors[-6:]))

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise LLMError("cancelled")

    @staticmethod
    def _sleep(seconds: float, cancel: threading.Event | None) -> None:
        if cancel is not None:
            if cancel.wait(seconds):
                raise LLMError("cancelled")
        else:
            time.sleep(seconds)

    def _used(self, p: Provider, model: str, msg: dict) -> tuple[dict, Provider]:
        used = dataclasses.replace(p, model=model)
        self.last_used = used
        return msg, used

    def complete(self, system: str, user: str) -> str:
        """One focused request without tools or chat history (fits even small free plans)."""
        msg, _ = self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}], tools=None)
        return msg.get("content") or ""

    def _try(self, p: Provider, model: str, messages, tools, trim, errors) -> tuple[str, object]:
        """Returns ('ok', message) | ('rate', seconds) | ('gone'|'auth'|'next_model'|'next_provider', None)."""
        if p.name == "kiro":
            return self._try_kiro(p, model, messages, tools, trim, errors)
        reasoning = p.model_reasoning.get(model, p.reasoning_effort)
        tool_retries, shrink, too_large = 1, 1.0, 0
        for _ in range(6):
            budget_tokens = self.budget_tokens(p, model)
            system_for = getattr(self, "_system_for", None)
            if system_for and messages and messages[0].get("role") == "system":
                messages = [{**messages[0], "content": system_for(budget_tokens)}] + list(messages[1:])
            if callable(tools):
                try:
                    tool_list = tools(p, budget_tokens)
                except TypeError:  # older callbacks take only the provider
                    tool_list = tools(p)
            else:
                tool_list = tools
            budget = p.context_chars
            if budget_tokens:
                tool_tokens = int(len(json.dumps(tool_list)) / TOOL_CHARS_PER_TOKEN) if tool_list else 0
                budget = min(budget, max(3000, int((budget_tokens - tool_tokens - 120) * TEXT_CHARS_PER_TOKEN)))
            msgs = trim(messages, int(budget * shrink)) if trim else messages
            kwargs = {"model": model, "messages": prepare_messages(p, msgs)}
            if tool_list:
                kwargs["tools"] = tool_list
                kwargs["tool_choice"] = "auto"
            if reasoning:
                kwargs["reasoning_effort"] = reasoning
            try:
                raw = self._client(p).chat.completions.with_raw_response.create(**kwargs)
                resp = raw.parse()
                self._learn_headers(p, model, raw.headers)
                if not resp.choices:
                    errors.append(f"{p.name}:{model} returned no answer")
                    return "next_model", None
                return "ok", _message_to_dict(resp.choices[0].message)
            except openai.RateLimitError as exc:
                if _is_too_large(exc):
                    shrink, too_large = self._shrink_for(p, model, exc, budget_tokens, shrink), too_large + 1
                    continue
                seconds = _retry_after(exc, 20.0)
                errors.append(f"{p.name}:{model} {_limit_kind(exc)} rate limit reached (retry in {int(seconds)}s)")
                return "rate", seconds
            except openai.AuthenticationError:
                errors.append(f"{p.name}: API key rejected (check Settings)")
                return "auth", None
            except openai.PermissionDeniedError:
                errors.append(f"{p.name}:{model} not allowed for this account")
                return "gone", None
            except openai.NotFoundError:
                errors.append(f"{p.name}: model '{model}' not found")
                return "gone", None
            except openai.BadRequestError as exc:
                low = str(exc).lower()
                if "tool_use_failed" in low or "failed_generation" in low or "failed to call a function" in low:
                    if tool_retries > 0:
                        tool_retries -= 1
                        continue
                    errors.append(f"{p.name}:{model} produced an invalid tool call")
                    return "next_model", None
                if reasoning and ("reasoning" in low or "thinking" in low):
                    reasoning = None
                    p.model_reasoning[model] = None
                    continue
                if "context" in low and ("length" in low or "too long" in low or "maximum" in low or "window" in low):
                    match = _CTX_NUM_RE.search(str(exc))
                    if match:
                        self._learn(p, model, ctx=int(match.group(1)))
                    shrink *= 0.6
                    continue
                if _is_too_large(exc):
                    shrink, too_large = self._shrink_for(p, model, exc, budget_tokens, shrink), too_large + 1
                    continue
                if "model" in low and ("not found" in low or "does not exist" in low or "decommissioned" in low
                                       or "invalid model" in low or "unknown model" in low):
                    errors.append(f"{p.name}: model '{model}' unavailable")
                    return "gone", None
                errors.append(f"{p.name}:{model} bad request: {str(exc)[:300]}")
                return "next_model", None
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                errors.append(f"{p.name}: cannot connect ({type(exc).__name__})")
                return "next_provider", None
            except openai.APIStatusError as exc:
                if exc.status_code == 413 or _is_too_large(exc):  # one request bigger than the plan allows
                    shrink, too_large = self._shrink_for(p, model, exc, budget_tokens, shrink), too_large + 1
                    continue
                errors.append(f"{p.name}: HTTP {exc.status_code}")
                if exc.status_code >= 500:
                    time.sleep(2)
                    continue
                return "next_provider", None
        if too_large:
            errors.append(f"{p.name}:{model}: request too large for this plan's per-minute limit even after shortening")
        else:
            errors.append(f"{p.name}:{model}: no usable answer after several tries")
        return "next_model", None

    def _try_kiro(self, p: Provider, model: str, messages, tools, trim, errors) -> tuple[str, object]:
        from . import kiro_bridge
        budget_tokens = self.budget_tokens(p, model)
        system_for = getattr(self, "_system_for", None)
        if system_for and messages and messages[0].get("role") == "system":
            messages = [{**messages[0], "content": system_for(budget_tokens)}] + list(messages[1:])
        if callable(tools):
            try:
                tool_list = tools(p, budget_tokens)
            except TypeError:
                tool_list = tools(p)
        else:
            tool_list = tools
        tool_chars = len(kiro_bridge.tool_lines(tool_list or []))
        budget = p.context_chars
        if budget_tokens:
            budget = min(budget, max(4000, int((budget_tokens - 600) * TEXT_CHARS_PER_TOKEN) - tool_chars))
        msgs = trim(messages, budget) if trim else messages
        try:
            bridge = kiro_bridge.bridge_for(p.api_key, getattr(LANE, "name", "main"))
            text = bridge.prompt(kiro_bridge.render_prompt(msgs, tool_list), model, timeout=p.timeout)
        except kiro_bridge.KiroError as exc:
            errors.append(f"kiro:{model}: {exc}")
            return {"auth": ("auth", None), "missing": ("next_provider", None), "model": ("gone", None),
                    "limit": ("rate", 300.0)}.get(exc.kind, ("next_model", None))
        if not text:
            errors.append(f"kiro:{model}: empty answer")
            return "next_model", None
        msg = kiro_bridge.parse_reply(clean_text(text))
        msg["content"] = clean_text(msg.get("content") or "")
        return "ok", msg

    def _shrink_for(self, p: Provider, model: str, exc: Exception, current: int | None, shrink: float) -> float:
        """Learn the plan's real limit from the error and shrink the next attempt by how much we were over."""
        self._learn_too_large(p, model, exc, current)
        m = re.search(r"Limit\s*[:=]?\s*(\d+)\D+Requested\s*[:=]?\s*(\d+)", str(exc), re.I)
        if m and int(m.group(2)) > 0:
            return shrink * max(0.3, min(0.9, int(m.group(1)) / int(m.group(2)) * 0.85))
        return shrink * 0.7

    def _learn_too_large(self, p: Provider, model: str, exc: Exception, current: int | None) -> None:
        match = _LIMIT_NUM_RE.search(str(exc))
        if match:
            self._learn(p, model, max=int(int(match.group(1)) * 0.7))
        else:  # no number given: shrink what we tried by 30%
            self._learn(p, model, max=int((current or 30_000) * 0.7))



def classify_plan(tpm: int | None, rpd: int | None, free_flag: bool | None) -> str:
    """'free' (small limits -> small requests, pauses), 'paid' (big limits -> full speed) or 'unknown'."""
    if free_flag is True:
        return "free"
    if tpm:
        return "free" if tpm < 30_000 else "paid"
    if free_flag is False:
        return "paid"
    if rpd and rpd <= 2_000:
        return "free"
    return "unknown"


def probe_provider(client: "LLMClient", p: Provider) -> dict:
    """One tiny request to check a key right after it's added: works or not, chosen model, limits and plan type.
    What it learns (tokens per minute, context window) is saved, so the agent sizes every request to fit."""
    out: dict = {"provider": p.name, "title": p.label_name or p.name, "ok": False}
    if p.name == "kiro":
        from . import kiro_bridge
        bridge = kiro_bridge.bridge_for(p.api_key)
        before, started = bridge.credits, time.time()
        try:
            reply = bridge.prompt("Reply with exactly: OK", p.model, timeout=120)
        except kiro_bridge.KiroError as exc:
            out["error"] = str(exc)
            return out
        out.update({"ok": bool(reply), "model": p.model, "plan": "paid", "tokens_per_minute": None,
                    "budget_tokens": client.budget_tokens(p, p.model),
                    "summary": (f"Kiro subscription: answered in {time.time() - started:.1f}s, used "
                                f"{bridge.credits - before:.4f} Kiro credits. No per-minute limit, so Karya runs at full speed "
                                "and only uses a little of your Kiro credits per step.")})
        return out
    try:
        model = client._resolve(p)
    except Exception as exc:  # noqa: BLE001 - report any failure to the user
        out["error"] = f"could not list models: {type(exc).__name__}"
        return out
    if not model:
        out["error"] = f"couldn't pick a model automatically; set {p.name.upper()}_MODEL in Settings"
        return out
    out["model"] = model
    free_flag = None
    if "openrouter.ai" in p.base_url:
        try:
            info = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {p.api_key}"},
                                timeout=15).json().get("data") or {}
            free_flag = info.get("is_free_tier")
            out["credits_limit"] = info.get("limit")
        except Exception:  # noqa: BLE001
            pass
    try:
        raw = client._client(p).chat.completions.with_raw_response.create(
            model=model, max_tokens=8, messages=[{"role": "user", "content": "Reply with OK"}])
        raw.parse()
        headers = raw.headers
        client._learn_headers(p, model, headers)
        out["ok"] = True
    except openai.AuthenticationError:
        out["error"] = "the key was rejected"
        return out
    except openai.RateLimitError as exc:
        out["ok"] = True  # the key works, it's just busy/limited right now
        out["note"] = "rate limited right now"
        headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    except openai.APIStatusError as exc:
        out["error"] = f"HTTP {exc.status_code}: {str(exc)[:160]}"
        return out
    except (openai.APIConnectionError, openai.APITimeoutError):
        out["error"] = "cannot connect"
        return out

    def num(name):
        try:
            return int(headers.get(name) or 0) or None
        except (TypeError, ValueError):
            return None

    tpm, req_limit = num("x-ratelimit-limit-tokens"), num("x-ratelimit-limit-requests")
    out.update({"tokens_per_minute": tpm, "request_limit": req_limit,
                "plan": classify_plan(tpm, req_limit if p.name == "groq" else None, free_flag),
                "budget_tokens": client.budget_tokens(p, model)})
    out["summary"] = {
        "free": "free/small plan: Karya keeps each request small and pauses briefly when the limit is reached",
        "paid": "large plan: Karya uses the full context and all tools at full speed",
        "unknown": "works; limits will be learned automatically while you use it",
    }[out["plan"]]
    return out