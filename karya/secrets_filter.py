"""Mask secrets in anything shown to the AI, the chat or the logs.

A command like `type .env` or `Get-Content vault.json` would otherwise put the user's API keys, the install token or
the vault's ciphertext straight into the model's context (and from there possibly to a cloud provider). Every tool
result passes through scrub() first. It masks known live secret values and well-known key shapes, and leaves ordinary
text alone."""
from __future__ import annotations

import re

# Well-known secret shapes (provider keys, cloud tokens, private keys).
_PATTERNS = [
    re.compile(r"\bksk_[A-Za-z0-9_-]{6,}"), re.compile(r"\bgsk_[A-Za-z0-9_-]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{12,}"), re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
# KEY=VALUE / KEY: VALUE lines in config dumps (mask the value, keep the key so the output still reads sensibly).
_ENV_LINE = re.compile(r"(?im)^(\s*[A-Za-z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|APP_PASSWORD)\s*[=:]\s*)(\S.*)$")


def _live_secrets() -> list[str]:
    """The actual secret values in use right now (so even a renamed/echoed copy is caught)."""
    import os
    values: list[str] = []
    try:
        from .config import settings
        for p in settings.providers:
            if getattr(p, "api_key", "") and len(p.api_key) >= 8 and p.api_key.lower() not in ("none", "ollama"):
                values.append(p.api_key)
        if settings.email_password:
            values.append(settings.email_password)
        if settings.vercel_token:
            values.append(settings.vercel_token)
    except Exception:  # noqa: BLE001 - never let scrubbing crash a result
        pass
    for name, value in os.environ.items():
        if re.search(r"(API_KEY|_TOKEN|_SECRET|PASSWORD)$", name, re.I) and value and len(value) >= 8:
            values.append(value)
    try:
        from .config import DATA_DIR
        token = (DATA_DIR / ".token").read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            values.append(token)
    except OSError:
        pass
    values += _vault_passwords()
    return values


_VAULT_CACHE: dict = {"stamp": None, "values": []}


def _vault_passwords() -> list[str]:
    """Saved login passwords (decrypted locally, cached until the vault file changes), so a password that ends up
    in a page, a log or a form answer is hidden before the AI or the chat sees it."""
    try:
        from . import vault
        path = vault.VAULT_FILE
        stamp = path.stat().st_mtime_ns if path.exists() else None
        if stamp != _VAULT_CACHE["stamp"]:
            found = []
            for account in vault.list_accounts():
                got = vault.get_secret(account.get("site", ""))
                if got and got[1] and len(got[1]) >= 8:
                    found.append(got[1])
            _VAULT_CACHE.update(stamp=stamp, values=found)
        return list(_VAULT_CACHE["values"])
    except Exception:  # noqa: BLE001 - never let scrubbing crash a result
        return []


def scrub(text):
    if not isinstance(text, str) or not text:
        return text
    for secret in _live_secrets():
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, "***hidden***")
    for pattern in _PATTERNS:
        text = pattern.sub("***hidden***", text)
    try:   # one-time login codes Karya typed (while they're still valid)
        from .login_codes import hide
        text = hide(text)
    except Exception:  # noqa: BLE001 - never let scrubbing crash a result
        pass
    return _ENV_LINE.sub(lambda m: m.group(1) + "***hidden***", text)
