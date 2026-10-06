"""Local account vault. Passwords are encrypted with Windows DPAPI (only this Windows user on this PC can decrypt
them) and are never sent to the AI model: the agent only sees site + username and asks the vault to type secrets."""
from __future__ import annotations

import base64
import ctypes
import json
import secrets as pysecrets
import string
import sys
import threading
import time
from urllib.parse import urlparse

from .config import DATA_DIR

VAULT_FILE = DATA_DIR / "vault.json"
_LOCK = threading.RLock()
_ENTROPY = b"karya-vault-v1"
ALIASES = {"x.com": ["twitter.com"], "twitter.com": ["x.com"], "gmail.com": ["google.com"], "google.com": ["gmail.com"],
           "workatastartup.com": ["ycombinator.com"], "ycombinator.com": ["workatastartup.com"]}
PRIMARY = "__primary__"   # the one login the user is happy to reuse to sign in / create accounts on many sites


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("The account vault uses Windows DPAPI and works on Windows only.")
    from ctypes import wintypes
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    in_buf, ent_buf = ctypes.create_string_buffer(data, len(data)), ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(in_buf, ctypes.c_void_p))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(ent_buf, ctypes.c_void_p))
    blob_out = _Blob()
    if not fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, 0x1, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _enc(text: str) -> str:
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode()


def _dec(blob: str) -> str:
    return _dpapi(base64.b64decode(blob), False).decode("utf-8")


def normalize_site(site: str) -> str:
    s = (site or "").strip().lower()
    if "://" in s:
        s = urlparse(s).netloc
    s = s.split("/")[0].split(":")[0]
    return s[4:] if s.startswith("www.") else s


def site_matches(site: str, host: str) -> bool:
    site, host = normalize_site(site), normalize_site(host)
    if not site or not host:
        return False
    for candidate in [site, *ALIASES.get(site, [])]:
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _load() -> dict:
    try:
        data = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"accounts": {}}
    except (OSError, json.JSONDecodeError):
        return {"accounts": {}}


def _save(data: dict) -> None:
    VAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(VAULT_FILE)


def list_accounts() -> list[dict]:
    with _LOCK:
        accounts = _load().get("accounts", {})
    return [{"site": "primary login (reused across sites)" if site == PRIMARY else site,
             "username": a.get("username", ""), "has_password": bool(a.get("secret")),
             "notes": a.get("notes", ""), "updated": a.get("updated", "")} for site, a in sorted(accounts.items())]


def save_account(site: str, username: str, password: str | None, notes: str = "") -> dict:
    key = normalize_site(site)
    if not key:
        raise ValueError("site is required")
    with _LOCK:
        data = _load()
        accounts = data.setdefault("accounts", {})
        entry = accounts.get(key, {"created": time.strftime("%Y-%m-%d %H:%M")})
        if password:
            if entry.get("secret"):
                entry["previous_secret"] = entry["secret"]
            entry["secret"] = _enc(password)
        entry["username"] = username or entry.get("username", "")
        if notes:
            entry["notes"] = notes
        entry["updated"] = time.strftime("%Y-%m-%d %H:%M")
        accounts[key] = entry
        _save(data)
    return {"site": key, "username": entry["username"], "has_password": bool(entry.get("secret"))}


def find_account(site_or_host: str) -> tuple[str, dict] | None:
    with _LOCK:
        accounts = _load().get("accounts", {})
    key = normalize_site(site_or_host)
    if key in accounts:
        return key, accounts[key]
    for site, entry in accounts.items():
        if site_matches(site, key):
            return site, entry
    return None


PRIMARY_LABEL = "primary login"   # (PRIMARY constant is defined near the top)


def set_primary(username: str, password: str | None) -> dict:
    return save_account(PRIMARY, username, password, notes="Primary login reused across sites")


def primary() -> dict | None:
    with _LOCK:
        entry = _load().get("accounts", {}).get(PRIMARY)
    return entry if entry and entry.get("secret") else None


def get_secret(site: str) -> tuple[str, str] | None:
    """(username, password) - for internal use only (never returned to the model).
    When reuse_login is on, a site with no saved account falls back to the primary login."""
    found = find_account(site)
    if found and found[1].get("secret"):
        return found[1].get("username", ""), _dec(found[1]["secret"])
    try:
        from .config import settings
        reuse = settings.reuse_login
    except Exception:  # noqa: BLE001
        reuse = False
    if reuse and normalize_site(site) != PRIMARY:
        entry = primary()
        if entry:
            return entry.get("username", ""), _dec(entry["secret"])
    return None


def delete_account(site: str) -> bool:
    key = normalize_site(site)
    with _LOCK:
        data = _load()
        if key in data.get("accounts", {}):
            del data["accounts"][key]
            _save(data)
            return True
    return False


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        pw = "".join(pysecrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*-_=+" for c in pw)):
            return pw
