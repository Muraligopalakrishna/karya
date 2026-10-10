"""Settings loaded from .env plus well-known paths."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
ENV_FILE = ROOT / ".env"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip().strip('"').strip("'")
    return value if value else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name, "")
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return max(0, int(_env(name, str(default))))
    except ValueError:
        return default


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)    # never printed in logs or error messages
    model: str                          # "" or "auto" = pick the best available model from the provider's /models list
    reasoning_effort: str | None = None
    context_chars: int = 400_000        # upper bound for what we send (chars); the real limit is learned per model
    timeout: float = 120.0
    fallback_models: tuple[str, ...] = ()
    model_reasoning: dict = field(default_factory=dict)  # per-model reasoning_effort overrides
    max_input_tokens: int | None = None  # starting per-request budget until the provider's real limit is known
    compact_tools: bool = False          # always send only the tool groups a request needs (small local models)
    slow: bool = False                   # last resort: wait for a rate-limited cloud model before using this
    prefer: tuple[str, ...] = ()         # regexes used to auto-pick a model, best first
    label_name: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}:{self.model or 'auto'}"


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    key_env: str
    base_url: str
    model: str = "auto"
    prefer: tuple[str, ...] = ()
    fallbacks: tuple[str, ...] = ()
    reasoning: str | None = None
    start_tokens: int | None = None      # conservative first guess for plans known to have tiny per-minute limits
    signup: str = ""
    free: bool = False


# Any OpenAI-compatible service works; these just need a key. Model "auto" = best model your key can use.
PRESETS: tuple[Preset, ...] = (
    Preset("kiro", "Kiro (your Kiro subscription, via Kiro CLI)", "KIRO_API_KEY", "kiro-cli://acp",
           model="qwen3-coder-next", fallbacks=("glm-5",), start_tokens=40_000,
           signup="https://app.kiro.dev/settings/api-keys"),
    Preset("openai", "OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1",
           prefer=(r"^gpt-[\d.]+-mini$", r"^gpt-[\d.]+$", r"^gpt-[\d.]+-[a-z]+$", r"^o\d+(-mini)?$"), reasoning="low",
           signup="https://platform.openai.com/api-keys"),
    Preset("anthropic", "Anthropic Claude", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/",
           prefer=(r"^claude-sonnet", r"sonnet", r"^claude-opus", r"^claude-haiku"), fallbacks=("claude-sonnet-4-6",),
           signup="https://console.anthropic.com/settings/keys"),
    Preset("gemini", "Google Gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/",
           model="gemini-3.8-flash", fallbacks=("gemini-3.7-flash", "gemini-2.5-flash"), reasoning="low",
           signup="https://aistudio.google.com/apikey", free=True),
    Preset("openrouter", "OpenRouter (400+ models, some free)", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
           model="openrouter/auto", fallbacks=("openrouter/free",), signup="https://openrouter.ai/keys", free=True),
    Preset("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", model="deepseek-chat",
           signup="https://platform.deepseek.com/api_keys"),
    Preset("mistral", "Mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", model="mistral-medium-latest",
           fallbacks=("mistral-large-latest", "mistral-small-latest"), signup="https://console.mistral.ai/api-keys", free=True),
    Preset("xai", "xAI Grok", "XAI_API_KEY", "https://api.x.ai/v1", prefer=(r"^grok-[\d.]+-fast", r"^grok-[\d.]+$", r"^grok"),
           signup="https://console.x.ai"),
    Preset("together", "Together AI", "TOGETHER_API_KEY", "https://api.together.xyz/v1",
           prefer=(r"gpt-oss-120b", r"(?i)qwen.*instruct", r"(?i)llama.*instruct"), signup="https://api.together.ai/settings/api-keys"),
    Preset("cerebras", "Cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1",
           prefer=(r"gpt-oss-120b", r"qwen", r"llama"), start_tokens=6_000, signup="https://cloud.cerebras.ai", free=True),
    Preset("groq", "Groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", model="openai/gpt-oss-120b",
           fallbacks=("openai/gpt-oss-20b", "qwen/qwen3.8-27b"), reasoning="low", start_tokens=6_000,
           signup="https://console.groq.com/keys", free=True),
)
PRESET_BY_NAME = {p.name: p for p in PRESETS}
CODEX_TITLE = "ChatGPT plan (via Codex CLI)"

# What a pasted key looks like -> which service it's from. Ambiguous shapes list several (the user picks).
KEY_SHAPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"^ksk_", ("kiro",)),
    (r"^sk-ant-", ("anthropic",)),
    (r"^sk-or-", ("openrouter",)),
    (r"^gsk_", ("groq",)),
    (r"^AIza[0-9A-Za-z_\-]{20,}$", ("gemini",)),
    (r"^xai-", ("xai",)),
    (r"^csk-", ("cerebras",)),
    (r"^tgp_", ("together",)),
    (r"^sk-(proj|svcacct|admin)-", ("openai",)),
    (r"^sk-[0-9a-f]{32}$", ("deepseek", "openai")),
    (r"^sk-", ("openai", "deepseek")),
    (r"^[0-9a-f]{64}$", ("together",)),
    (r"^[A-Za-z0-9]{32}$", ("mistral",)),
)


def detect_key(key: str) -> list[str]:
    """The services a pasted key can be from, most likely first ([] = unknown shape: ask the user)."""
    import re as _re
    key = (key or "").strip()
    for pattern, names in KEY_SHAPES:
        if _re.search(pattern, key):
            return list(names)
    return []


class Settings:
    """Mutable singleton; call reload() after .env changes."""

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> "Settings":
        if ENV_FILE.exists():
            load_dotenv(ENV_FILE, override=True)
        self.workspace = Path(_env("WORKSPACE_DIR", str(ROOT / "workspace")))
        self.resume_path = Path(_env("RESUME_PATH", str(ROOT / "resume.pdf")))
        self.approval_mode = _env("APPROVAL_MODE", "ask").lower()
        self.auto_submit_picked = _bool("AUTO_SUBMIT_PICKED", False)
        # Full access (autopilot): Karya does everything without asking. Quality/safety checks still run. A floor of
        # truly irreversible actions (real-money payments, deleting accounts/data) still asks unless the floor is off.
        self.full_access = _bool("KARYA_FULL_ACCESS", False)
        self.full_access_payments = _bool("KARYA_FULL_ACCESS_PAYMENTS", False)  # let autopilot pay/delete too
        self.full_access_cap = _int("KARYA_FULL_ACCESS_CAP", 60)                # most auto-approved big actions per run
        # Reuse one login the user gave (email + password) to sign in and create accounts, instead of asking per site.
        self.reuse_login = _bool("KARYA_REUSE_LOGIN", False)
        self.keep_going = _bool("KEEP_GOING", False)
        try:
            self.keep_going_minutes = max(5, int(_env("KEEP_GOING_MINUTES", "60")))
        except ValueError:
            self.keep_going_minutes = 60
        # AI apps (Claude, Cursor, Kiro, VS Code...) using Karya's tools over MCP
        self.mcp_enabled = _bool("KARYA_MCP_ENABLED", True)
        self.mcp_tools = _env("KARYA_MCP_TOOLS", "")
        # Tasks from the user's phone: their WhatsApp "Message yourself" chat (see karya/whatsapp.py)
        self.whatsapp_enabled = _bool("WHATSAPP_ENABLED", False)
        # Keep the PC from sleeping while Karya runs, so bots work on time (the screen can still turn off)
        self.keep_awake = _bool("KARYA_KEEP_AWAKE", False)
        # Daily limits that protect the user's accounts from being flagged as spam (any AI, any mode)
        self.max_emails_per_day = _int("KARYA_MAX_EMAILS_PER_DAY", 40)
        self.max_posts_per_day = _int("KARYA_MAX_POSTS_PER_DAY", 10)
        self.max_applications_per_day = _int("KARYA_MAX_APPLICATIONS_PER_DAY", 30)
        self.browser_channel = _env("BROWSER_CHANNEL", "chrome")
        self.browser_headless = _bool("BROWSER_HEADLESS", False)
        mode = _env("BROWSER_MODE", "auto").lower()
        self.browser_mode = mode if mode in ("auto", "chrome", "karya") else "auto"
        self.port = int(_env("PORT", "8765"))
        self.email_address = _env("EMAIL_ADDRESS")
        self.email_password = _env("EMAIL_APP_PASSWORD").replace(" ", "")
        self.smtp_host = _env("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(_env("SMTP_PORT", "465"))
        self.imap_host = _env("IMAP_HOST", "imap.gmail.com")
        self.vercel_token = _env("VERCEL_TOKEN")
        self.providers = self._build_providers()
        for d in (DATA_DIR, LOG_DIR, self.workspace):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def _build_providers(self) -> list[Provider]:
        known: dict[str, Provider] = {}
        if _env("CUSTOM_BASE_URL") and _env("CUSTOM_MODEL"):
            known["custom"] = Provider("custom", _env("CUSTOM_BASE_URL"), _env("CUSTOM_API_KEY", "none"),
                                       _env("CUSTOM_MODEL"), label_name="Custom")
        for preset in PRESETS:
            key = _env(preset.key_env)
            if preset.name == "kiro" and not key and _env("CUSTOM_API_KEY").startswith("ksk_"):
                key = _env("CUSTOM_API_KEY")  # Kiro keys pasted into the custom fields still work
            if preset.name == "kiro" and not key and _bool("KIRO_LOGIN", False):
                key = "login"                 # signed in to Kiro in Setup (no key): the Kiro CLI uses that login
            if not key:
                continue
            model = _env(f"{preset.name.upper()}_MODEL", preset.model)
            known[preset.name] = Provider(
                preset.name, preset.base_url, key, "" if model == "auto" else model,
                reasoning_effort=preset.reasoning, fallback_models=preset.fallbacks, prefer=preset.prefer,
                max_input_tokens=preset.start_tokens, label_name=preset.title,
                model_reasoning={"qwen/qwen3.8-27b": "none"} if preset.name == "groq" else {},
                **({"context_chars": 160_000, "timeout": 240.0} if preset.name == "kiro" else {}))
        if _bool("CODEX_ENABLED", False):    # a ChatGPT plan, signed in through the Codex CLI
            known["codex"] = Provider("codex", "codex-cli://exec", "login", _env("CODEX_MODEL", "gpt-5.4-mini"),
                                      reasoning_effort=_env("CODEX_REASONING", "low"), context_chars=200_000,
                                      timeout=240.0, fallback_models=("gpt-5.4",), max_input_tokens=50_000,
                                      label_name=CODEX_TITLE)
        if _bool("OLLAMA_ENABLED", True):
            known["ollama"] = Provider(
                "ollama", _env("OLLAMA_BASE_URL", "http://localhost:11434/v1"), "ollama",
                _env("OLLAMA_MODEL", "karya-qwen"), reasoning_effort="none",
                context_chars=26_000, timeout=600.0, fallback_models=("qwen3.5:4b",),
                compact_tools=True, slow=True, label_name="Ollama (offline)")
        order = [p.strip().lower() for p in _env("LLM_PROVIDERS").split(",") if p.strip()]
        if not order:  # default: the user's own gateway first, then subscriptions and paid APIs, free tiers, offline last
            order = ["custom", "kiro", "codex", *[p.name for p in PRESETS if p.name != "kiro"], "ollama"]
        return [known[n] for n in order if n in known]

    @property
    def email_ready(self) -> bool:
        return bool(self.email_address and self.email_password)

    def status(self) -> dict:
        return {
            "providers": [p.label for p in self.providers],
            "email": self.email_address if self.email_ready else None,
            "vercel": bool(self.vercel_token),
            "resume": str(self.resume_path) if self.resume_path.exists() else None,
            "workspace": str(self.workspace),
            "approval_mode": self.approval_mode,
            "auto_submit_picked": self.auto_submit_picked,
            "keep_going": self.keep_going,
            "full_access": self.full_access,
            "full_access_payments": self.full_access_payments,
            "reuse_login": self.reuse_login,
        }


settings = Settings()

EDITABLE_KEYS = tuple([k for p in PRESETS for k in (p.key_env, f"{p.name.upper()}_MODEL")]
                     + ["CUSTOM_BASE_URL", "CUSTOM_API_KEY", "CUSTOM_MODEL", "LLM_PROVIDERS",
                        "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "VERCEL_TOKEN", "RESUME_PATH", "BROWSER_MODE",
                        "APPROVAL_MODE", "AUTO_SUBMIT_PICKED", "KEEP_GOING", "KEEP_GOING_MINUTES",
                        "KARYA_MCP_ENABLED", "KARYA_MCP_TOOLS", "KARYA_MAX_EMAILS_PER_DAY", "KARYA_MAX_POSTS_PER_DAY",
                        "KARYA_MAX_APPLICATIONS_PER_DAY", "KARYA_FULL_ACCESS", "KARYA_FULL_ACCESS_PAYMENTS",
                        "KARYA_FULL_ACCESS_CAP", "KARYA_REUSE_LOGIN", "WHATSAPP_ENABLED",
                        "KIRO_LOGIN", "CODEX_ENABLED", "CODEX_MODEL", "CODEX_REASONING", "OLLAMA_MODEL",
                        "KARYA_KEEP_AWAKE"])
SECRET_KEYS = {p.key_env for p in PRESETS} | {"CUSTOM_API_KEY", "EMAIL_APP_PASSWORD", "VERCEL_TOKEN"}


def provider_order(first: str | None = None, add: str | None = None, drop: str | None = None) -> str:
    """The LLM_PROVIDERS value after a change: `first` moves to the front, `add` joins (so a newly connected AI is
    never left out of a custom order), `drop` leaves."""
    current = [p.name for p in settings.providers]
    names = list(dict.fromkeys(([first] if first else []) + current + ([add] if add else [])))
    return ",".join(n for n in names if n and n != drop)


def provider_catalog() -> list[dict]:
    """What the Settings form needs to show one row per AI provider."""
    rows = [{"name": p.name, "title": p.title, "key_env": p.key_env, "model_env": f"{p.name.upper()}_MODEL",
             "default_model": p.model, "signup": p.signup, "free": p.free} for p in PRESETS]
    return rows


def settings_view() -> dict:
    """What the Settings form shows: secrets only as set/not set, other values in full."""
    out = {}
    for key in EDITABLE_KEYS:
        value = _env(key)
        out[key] = {"set": bool(value), "value": "" if key in SECRET_KEYS else value}
    return {"values": out, "providers": provider_catalog()}


def update_env(updates: dict) -> list[str]:
    """Write keys into .env (creating it from .env.example if needed) and reload settings."""
    import re as _re
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    else:
        example = ROOT / ".env.example"
        lines = example.read_text(encoding="utf-8").splitlines() if example.exists() else []
    changed = []
    for key, value in updates.items():
        if key not in EDITABLE_KEYS:
            raise ValueError(f"{key} can't be changed here")
        value = str(value if value is not None else "").strip()
        if "\n" in value or "\r" in value or len(value) > 500:
            raise ValueError(f"invalid value for {key}")
        for i, line in enumerate(lines):
            if _re.match(rf"^\s*{key}\s*=", line):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
        os.environ[key] = value
        changed.append(key)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings.reload()
    return changed
