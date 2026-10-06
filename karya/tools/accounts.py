"""Account tools. The model sees only site + username; passwords stay in the encrypted vault and are typed into pages
by browser_type_secret (which refuses to type a password into a different website than the one it belongs to)."""
from __future__ import annotations

from .. import vault
from ..registry import CONFIRM, P, tool


@tool("list_accounts", "List the user's saved accounts (site + username; passwords are never shown). "
      "Check this before logging in anywhere.", group="accounts")
def list_accounts():
    rows = vault.list_accounts()
    return rows or "No saved accounts yet. Use request_credentials to ask the user, or vault_new_password to create one for a sign-up."


@tool("request_credentials", "Ask the user to enter a login (username/email + password) for a site in a secure form. "
      "The password goes straight into the encrypted vault; you never see it. Use when a site needs a login and "
      "list_accounts has nothing for it. A saved login is never asked again (use browser_type_secret).", {
    "site": P("string", "Website, e.g. linkedin.com"),
    "reason": P("string", "Why you need it, e.g. 'to apply with Easy Apply'"),
    "wrong_password": P("boolean", "Only when the site rejected the SAVED password"),
}, required=["site"], group="accounts")
def request_credentials(site: str, reason: str = "", wrong_password: bool = False):
    return "ERROR: request_credentials only works from the chat (the user must fill a secure form)."


@tool("vault_new_password", "Make an account for signing up on a site: saves a username and password in the vault, then "
      "type them with browser_type_secret. With 'reuse login' on it uses the user's primary login, so you don't ask; "
      "otherwise it generates a strong password Karya stores.", {
    "site": P("string", "Website, e.g. workatastartup.com"),
    "username": P("string", "Username or email (leave empty to use the primary login's email)"),
}, required=["site"], group="accounts")
def vault_new_password(site: str, username: str = ""):
    from ..config import settings
    existing = vault.find_account(site)
    if existing and existing[1].get("secret"):
        return (f"An account for {existing[0]} already exists (username: {existing[1].get('username')}). "
                "Use browser_type_secret with it instead of creating a new password.")
    prim = vault.primary()
    if not username and prim:
        username = prim.get("username", "")
    if not username:
        if settings.reuse_login:
            return ("ERROR: no primary login saved yet. Ask the user once with request_credentials(site=\"primary\") "
                    "(or they add it in Setup), then Karya reuses it for sign-ups.")
        return "ERROR: give a username/email for the new account."
    # reuse_login: use the primary password everywhere (the user's choice); otherwise a unique strong one Karya stores.
    password = vault._dec(prim["secret"]) if (settings.reuse_login and prim) else vault.generate_password()
    saved = vault.save_account(site, username, password, notes="reused primary login" if (settings.reuse_login and prim)
                               else "created by Karya for sign-up")
    how = "your usual login" if (settings.reuse_login and prim) else "a strong saved password"
    return f"Saved {how} for {saved['site']} (username: {saved['username']}). Now use browser_type_secret."


@tool("forget_account", "Delete a saved account from the vault.", {"site": P("string", "Website")},
      required=["site"], risk=CONFIRM, group="accounts", summary=lambda a: f"Delete saved login for {a.get('site')}")
def forget_account(site: str):
    return "Deleted." if vault.delete_account(site) else f"No saved account for {site}."
