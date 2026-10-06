"""Full access (autopilot): Karya does everything without asking. This module draws the line.

Even in full access, a short list of truly irreversible actions still asks the user first, unless they also turn the
floor off: real-money payments, deleting accounts or data, and typing a saved password into a site it doesn't belong
to (a phishing guard). Everything else - posting, applying, sending email, filling forms, running commands - runs on
its own, while Karya's quality checks (empty fields, duplicates, limits, verification) still apply."""
from __future__ import annotations

import re

PAYMENT = re.compile(
    r"\b(pay|paying|pay now|buy|buying|purchase|checkout|check out|place (the |your )?order|order now|"
    r"complete (the )?(order|purchase|payment)|subscribe|subscription|upgrade (to )?(pro|premium|plan)|"
    r"add (a )?(card|payment)|confirm (and )?pay|donate|send money|transfer|wire|top ?up|recharge|"
    r"\bbid\b.*\b(fee|credit)|enter (card|cvv|upi|otp for payment))\b|[₹$€£]\s?\d|\bcheckout\b|\bbilling\b", re.I)
PAY_URL = re.compile(r"checkout|/pay(ment)?\b|billing|/cart|/subscribe|/upgrade|/order|razorpay|stripe|paypal|/donate", re.I)
DELETE_ACCOUNT = re.compile(
    r"\b(delete|close|deactivate|cancel|terminate|remove|wipe|permanently (delete|remove)|erase) "
    r"(my |your |the )?(account|profile|membership|subscription|workspace|organ[ia]zation|all data|everything|repo)", re.I)
DESTRUCTIVE_TOOLS = {"delete_path", "delete_account", "forget_account"}


def must_ask(name: str, level: str, summary: str, args: dict, include_payments: bool = False) -> str | None:
    """Why this action still needs the user's OK even in full access, or None if autopilot may run it.
    include_payments=True (the user turned the floor off) lets payments/deletes through too."""
    if include_payments:
        return None
    text = f"{summary or ''} {args.get('text') or ''} {args.get('option') or ''}".strip()
    host = str(args.get("url") or "")
    if name == "browser_type_secret" and level == "critical":
        return "typing your saved password into a site it doesn't belong to"   # _secret_risk flags only mismatches
    if PAYMENT.search(text) or PAY_URL.search(host) or PAY_URL.search(summary or ""):
        return "a payment"
    if name in DESTRUCTIVE_TOOLS:
        return "deleting a file or account"
    if DELETE_ACCOUNT.search(text):
        return "deleting or closing an account"
    return None
