"""Browser automation in two ways, with the same tools:
- Your own Chrome, through the Karya Browser Link extension: Karya opens its own tab (in a "Karya" tab group) and
  works there, with all your normal logins. Used whenever the extension is connected (BROWSER_MODE=auto or chrome).
- Karya's own Chrome window (Playwright, persistent profile in data/browser_profile), when the extension isn't
  installed or BROWSER_MODE=karya.
The model sees numbered elements (with the question each form field belongs to) and acts on them."""
from __future__ import annotations

import base64
import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from ..config import DATA_DIR, settings
from ..ext_link import EXTENSION_DIR, ExtensionUnavailable, link
from ..registry import CONFIRM, CRITICAL, SAFE, P, tool

PAGE_JS = (EXTENSION_DIR / "page.js").read_text(encoding="utf-8")
SNAPSHOT_JS = "(args) => {\n" + PAGE_JS + "\nreturn window.__karya.snapshot(args);\n}"
PAGE_CALL_JS = "(a) => {\n" + PAGE_JS + "\nreturn window.__karya[a.fn](a.args);\n}"

_DEAD_BROWSER = re.compile(r"connection closed|has been closed|target closed|browser closed|not connected|"
                           r"closed while reading|pipe|disconnected", re.I)
_CRITICAL_WORDS = re.compile(
    r"\b(post|publish|tweet|reply|send|submit|apply|share|pay|buy|purchase|order|checkout|delete|remove|confirm|"
    r"donate|transfer|subscribe|unsubscribe|bid|comment|repost|follow|connect|withdraw|accept|decline|"
    r"sign ?up|register|create account|book now|place)\b", re.I)
_MEDIUM_WORDS = re.compile(r"\b(like|upvote|downvote|react|save|update|edit|invite|join|rsvp|enrol+|message)\b", re.I)
_HARMLESS = re.compile(r"\b(search|find|go|filter|show results|next|previous|continue|back|close|cancel|dismiss|"
                       r"ok|got it|skip|not now|later|log ?in|sign ?in)\b", re.I)
# Buttons that only OPEN an editor ("Start a post", "Add a comment") - the real Post/Send click is still gated.
_OPENER = re.compile(r"^(start|create|write|new|add|compose)\s+(a\s+|an\s+|new\s+)?(post|tweet|message|comment|reply)\b", re.I)
# On these sites an "Apply" button only opens the application form; the real send is "Submit application".
_FORM_HOSTS = re.compile(r"(^|\.)(linkedin\.com|greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|workable\.com|"
                         r"smartrecruiters\.com|ycombinator\.com|workatastartup\.com)$", re.I)
_APPLY_OPENER = re.compile(r"^\s*(easy apply|apply|apply now|apply for this (job|position|role)|apply to this job|"
                           r"apply on company (site|website)|i'?m interested)\b", re.I)
# Clicks that submit a form or an application: Karya checks the form before and the page after.
_SUBMIT_LIKE = re.compile(r"\b(submit|apply|application|finish|complete)\b", re.I)
# Errors that mean "the page started loading something new while the action ran" (so the action did happen).
_NAVIGATED = re.compile(r"page changed while|unloaded|navigat|frame with id|frame.*removed|showing error page", re.I)


def classify_click(item: dict | None, label: str, url: str = "") -> str:
    text = (label or "").strip()
    host = urlparse(url or "").netloc.lower()
    if _APPLY_OPENER.search(text) and host and _FORM_HOSTS.search(host) and not re.search(r"submit|send", text, re.I):
        return CONFIRM
    if _OPENER.search(text):
        return SAFE
    if _CRITICAL_WORDS.search(text):
        return CRITICAL
    if item and item.get("tag") in ("button", "input") and re.search(r"checkout|payment|/pay\b|billing", url or "", re.I):
        return CRITICAL
    if item and item.get("type") == "submit" and not _HARMLESS.search(text):
        return CONFIRM if text else CRITICAL
    if _MEDIUM_WORDS.search(text):
        return CONFIRM
    return SAFE


_BID = re.compile(r"\b(place|submit|update|send|confirm)\s+(my\s+|a\s+|the\s+|your\s+)?bid\b|\bbid now\b", re.I)
_POST = re.compile(r"^\W*(post|post now|tweet|tweet all|reply|repost|publish|share|share now|share post)\W*$", re.I)


def is_post_click(item: dict | None, label: str, url: str = "") -> bool:
    """The final Post/Share/Publish button of a social composer."""
    return classify_click(item, label, url) == CRITICAL and bool(_POST.search(label or "")) and not \
        re.search(r"career|job|apply|recruit|talent|hiring", urlparse(url or "").path + urlparse(url or "").netloc, re.I)


def is_submit_click(item: dict | None, label: str, url: str = "") -> bool:
    if classify_click(item, label, url) != CRITICAL:
        return False
    if _SUBMIT_LIKE.search(label or "") or _BID.search(label or "") or is_post_click(item, label, url):
        return True
    # "Send" on a careers / job page sends an application too
    return bool(re.search(r"\bsend\b", label or "", re.I) and re.search(r"career|job|apply|recruit|talent|hiring",
                                                                      urlparse(url or "").path + urlparse(url or "").netloc, re.I))


_CAPTCHA = re.compile(r"not a robot|captcha|recaptcha|hcaptcha|turnstile|verify you are human|i am human", re.I)


def captcha_problem(item: dict | None) -> str | None:
    """CAPTCHAs are for the user to solve; Karya never ticks them itself."""
    if item and _CAPTCHA.search(" ".join(str(item.get(k, "")) for k in ("label", "question", "name", "href"))):
        return ("NOT DONE: this is a CAPTCHA (\"I'm not a robot\"). Karya never solves those for the user. Ask the user "
                "to tick it themselves in the Karya tab, wait for them (browser_wait), then continue.")
    return None


def is_search_field(item: dict | None) -> bool:
    if not item:
        return False
    text = " ".join(str(item.get(k, "")) for k in ("label", "placeholder", "name")).lower()
    return (item.get("type") == "search" or item.get("role") == "searchbox"
            or item.get("name") in ("q", "query", "search", "keywords") or bool(re.search(r"search|find|query", text)))


_PLACE = re.compile(r"location|city|town|address|country|region", re.I)


def wants_pick(item: dict | None) -> bool:
    """Text fields whose value should be chosen from the page's suggestion list (comboboxes, location fields)."""
    if not item or is_search_field(item) or item.get("tag") not in ("input", "textarea", None):
        return False
    return item.get("role") == "combobox" or bool(_PLACE.search(f"{item.get('label', '')} {item.get('name', '')}"))


# ---------------------------------------------------------------- what happened after a submit click
_OK_TEXT = re.compile(
    r"thank(?:s| you)\b[^.\n]{0,60}\b(?:appl|submi|interest)|application[^.\n]{0,40}\b(?:received|submitted|was sent|"
    r"has been sent|is on its way|is complete)|\bwe(?:'ve| have)? (?:have )?received your|successfully (?:submitted|applied|sent)|"
    r"you(?:'ve| have) (?:successfully )?applied|submission (?:received|successful)|application submitted|"
    r"your (?:bid|proposal) (?:has been|was) (?:placed|submitted|updated|sent)|(?:bid|proposal) (?:placed|submitted|updated) "
    r"successfully|you(?:'ve| have) (?:placed|submitted) (?:a|your) (?:bid|proposal)|\bretract (?:your )?bid\b|"
    r"your (?:bid|proposal) is (?:live|active|now)|"
    r"your (?:post|tweet|reel|story|reply|comment|update|message) (?:was|has been|is) (?:sent|shared|published|posted|live)|"
    r"\bpost(?:ed)? successfully\b|\bpost successful\b|\b(?:reel|post|video) (?:shared|published)\b|\bview (?:your )?post\b|"
    r"\byour post is now live\b", re.I)
_BAD_TEXT = re.compile(
    r"needs? corrections?|missing (?:entry|required)|required field|\b(?:is|are) required\b|this field is required|"
    r"can(?:'|no)?t be (?:blank|empty)|please (?:fill|complete|enter|select|provide|correct|fix|upload|answer|choose)|"
    r"\binvalid\b|something went wrong|an error (?:occurred|has occurred)|error (?:submitting|sending)|"
    r"(?:failed|unable) to (?:submit|send)|couldn'?t (?:submit|send)|please try again|"
    r"(?:not enough|no more|no remaining|out of|used (?:all|up)(?: of)?(?: your)?) bids|you have 0 bids|"
    r"bids? (?:remaining|left)\W*0\b|upgrade (?:your )?(?:membership|plan)|subscribe to (?:a|the)\b[^.\n]{0,40}"
    r"(?:plan|membership)|\binsufficient\b|bid limit|you can(?:no|')?t (?:place a )?bid|unable to (?:place|submit) "
    r"(?:a |your )?bid|verify your (?:email|identity|account|phone)", re.I)
_OK_URL = re.compile(r"thank|confirm|success|submitted|applied|complete", re.I)


def submit_verdict(before_url: str, before_text: str, after_url: str, after_text: str) -> str:
    """SUBMITTED / NOT SUBMITTED / UNCONFIRMED, from what is new on the page after the click."""
    old = {line.strip() for line in (before_text or "").splitlines() if line.strip()}
    new = [line.strip() for line in (after_text or "").splitlines()
           if line.strip() and line.strip() not in old and len(line.strip()) <= 220]
    bad = [line for line in new if _BAD_TEXT.search(line)]
    good = [line for line in new if _OK_TEXT.search(line)]
    if bad and good:
        return (f'RESULT: UNCONFIRMED - the page says "{good[0][:140]}" but also "{bad[0][:140]}". Read the page '
                "(browser_read_text) before telling the user anything.")
    if bad:
        shown = " | ".join(f'"{line[:150]}"' for line in bad[:4])
        return (f"RESULT: NOT SUBMITTED - the page shows problems: {shown}. Fix those fields (ask the user for anything "
                "you don't know), then submit again. Don't tell the user it was submitted.")
    if good:
        return f'RESULT: SUBMITTED - the page now says: "{good[0][:160]}"'

    def where(url: str) -> tuple[str, str]:
        parsed = urlparse(url or "")
        return parsed.netloc, parsed.path.rstrip("/")

    if where(after_url) != where(before_url) and _OK_URL.search(urlparse(after_url or "").path):
        return f"RESULT: SUBMITTED - the page moved to a confirmation address ({after_url[:120]})."
    return ("RESULT: UNCONFIRMED - no confirmation or error message appeared. Don't tell the user it was submitted: "
            "read the page (browser_read_text) and look for a confirmation or errors first.")


# ---------------------------------------------------------------- snapshot text the model reads
_PIECE_NAMES = {"k": "K", "q": "Q", "r": "R", "b": "B", "n": "N", "p": "P"}


def _pieces_text(pieces: dict) -> str:
    sides = {"White": [], "Black": []}
    order = "kqrbnp"
    for square, piece in sorted(pieces.items(), key=lambda kv: (order.index(kv[1].lower()), kv[0])):
        sides["White" if piece.isupper() else "Black"].append(f"{_PIECE_NAMES[piece.lower()]}{square}")
    return "; ".join(f"{side}: {' '.join(items)}" for side, items in sides.items() if items)


def _fmt(it: dict) -> str:
    if it.get("area"):
        line = f"[{it['id']}] area {it['tag']} \"{it.get('label', '')}\" {it.get('size', '')}"
        if it.get("board") == "chess":
            side = "Black" if it.get("flipped") else "White"
            line += (f" = CHESS BOARD ({it.get('site')}; {side} is at the bottom). Position: {_pieces_text(it.get('pieces') or {})}."
                     " Move with browser_move_piece(from_square, to_square), e.g. e2, e4.")
        else:
            line += " - click or drag inside it with browser_click_at / browser_drag (x, y as 0-1 fractions of its box)"
        if it.get("offscreen"):
            line += " (offscreen)"
        return line
    kind = it.get("role") or it["tag"]
    if it["tag"] == "input":
        kind = f"input[{it.get('type', 'text')}]"
        if it.get("role") == "combobox":
            kind += " combobox"
    if it.get("editable"):
        kind = "editor"
    line = f"[{it['id']}] {kind} \"{it.get('label', '')}\""
    if it.get("value") not in (None, ""):
        line += f" value=\"{it['value']}\""
    if it.get("placeholder") and it.get("placeholder") != it.get("label"):
        line += f" placeholder=\"{it['placeholder']}\""
    if "checked" in it:
        line += " [x]" if it["checked"] else " [ ]"
    if it.get("options"):
        line += " options=" + "|".join(it["options"][:12])
    if it.get("href") and not it["href"].startswith("javascript"):
        line += f" -> {it['href'][:60]}"
    if it.get("question"):
        line += f" (question: {it['question'][:140]})"
    if it.get("disabled"):
        line += " (disabled)"
    if it.get("invalid"):
        line += " (marked invalid)"
    if it.get("required"):
        line += " *required"
    return line


def format_snapshot(url: str, title: str, items: list[dict], events: list[str], text: str, max_items: int,
                    text_chars: int, filter_text: str | None = None, where: str = "") -> str:
    shown = items
    if filter_text:
        words = [w.strip().lower() for w in re.split(r"[,|]", filter_text) if w.strip()]
        shown = [it for it in items if any(w in " ".join(str(it.get(k, "")) for k in ("label", "placeholder", "href", "question")).lower()
                                           for w in words)]
    lines = [f"URL: {url}", f"Title: {title}"]
    if where:
        lines.append(where)
    if events:
        lines.append("Events: " + " | ".join(events[-5:]))
    lines.append(f"Interactive elements ({len(shown)} of {len(items)}; use the [id] numbers):")
    lines.extend(_fmt(it) for it in shown[:max_items])
    if len(shown) > max_items:
        lines.append(f"...{len(shown) - max_items} more (scroll, or browser_snapshot with filter)")
    text = re.sub(r"\n\s*\n+", "\n", text or "").strip()
    if text_chars:
        lines.append(f"Page text (first {text_chars} chars):\n{text[:text_chars]}")
    return "\n".join(lines)


_YES_NO = {"yes", "no"}


def snap_result(found, next_id: int) -> tuple[list[dict], int]:
    """page.js returns {items, next}; an older extension script returns just the list. Returns (items, next free id)."""
    if isinstance(found, dict):
        items, nxt = list(found.get("items") or []), int(found.get("next") or 0)
    else:
        items, nxt = list(found or []), 0
    top = max([int(it.get("id") or 0) for it in items] + [next_id - 1])
    return items, max(nxt, top + 1)
_TRUTHY = {"true", "1", "on", "yes", "click", "x", "checked"}
_FALSY = {"false", "0", "off", "no", "", "unchecked"}


def field_action(item: dict, value) -> tuple[str, object]:
    """How browser_fill sets one field: ('select'|'check'|'upload'|'click'|'skip'|'type'|'fill', value)."""
    text = str(value)
    if item.get("tag") == "select":
        return "select", text
    if item.get("type") in ("checkbox", "radio") or item.get("role") in ("checkbox", "radio", "switch"):
        return "check", text.strip().lower() not in _FALSY
    if item.get("type") == "file":
        return "upload", text
    if item.get("tag") in ("button", "a") or item.get("role") in ("button", "option", "tab", "menuitem", "link"):
        label, wanted = (item.get("label") or "").strip().lower(), text.strip().lower()
        if wanted == label:
            return "click", None
        if label in _YES_NO and wanted in _YES_NO:
            raise ValueError(f"this is the \"{item.get('label')}\" button; use the id of the \"{text}\" button instead")
        if wanted in _TRUTHY:
            return "click", None
        if wanted in _FALSY:
            return "skip", None
        raise ValueError(f"this is a button (\"{item.get('label')}\"), not a text field; to choose an answer, use the "
                         "id of the button with that text")
    if item.get("editable") or (item.get("role") == "textbox" and item.get("tag") not in ("input", "textarea")):
        return "type", text
    return "fill", text


class _Common:
    """Shared by both backends."""
    items: dict
    url: str

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc or "the current page"

    def find_item(self, element_id=None, text: str | None = None) -> dict | None:
        if element_id is not None:
            try:
                return self.items.get(int(element_id))
            except (TypeError, ValueError):
                return None
        if text:
            wanted = text.strip().lower()
            for matcher in (lambda l: l == wanted, lambda l: l.startswith(wanted), lambda l: wanted in l):
                for it in self.items.values():
                    if matcher((it.get("label") or "").lower()):
                        return it
        return None

    def await_verdict(self, before: tuple[str, str], seconds: int = 8) -> str:
        """Poll the page for a confirmation or error message after a submit click."""
        verdict = ""
        for _ in range(seconds):
            self._pause(1.0)
            try:
                url, text = self.current_url(), self.all_text()
            except Exception:  # noqa: BLE001 - the page may be navigating
                continue
            verdict = submit_verdict(before[0], before[1], url, text)
            if not verdict.startswith("RESULT: UNCONFIRMED"):
                break
        return verdict or submit_verdict(before[0], before[1], "", "")

    # implemented by each backend
    def _pause(self, seconds: float) -> None: ...
    def current_url(self) -> str: ...
    def all_text(self) -> str: ...


# ================================================================ Karya's own Chrome window (Playwright)
class BrowserSession(_Common):
    """All Playwright calls run on one dedicated thread (the sync API is not thread-safe)."""

    kind = "karya"

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="karya-browser")
        self._pw = None
        self.context = None
        self.page = None
        self.items: dict[int, dict] = {}
        self.frames: dict[int, object] = {}
        self.url = ""
        self.title = ""
        self.events: list[str] = []
        self.last_typed: dict | None = None
        self.accept_confirms = False
        self.profile_dir = DATA_DIR / "browser_profile"
        self.next_id = 1      # element ids are never reused within a session
        self.fresh = True     # first snapshot: clear ids left by an earlier session
        self.last_options: list[str] = []

    def call(self, fn, *args, **kwargs):
        try:
            return self._executor.submit(fn, *args, **kwargs).result(timeout=300)
        except Exception as exc:
            if not _DEAD_BROWSER.search(str(exc)):
                raise
            # the user closed the window or the driver died: start a fresh browser and try once more
            self._executor.submit(self._reset).result(timeout=60)
            return self._executor.submit(fn, *args, **kwargs).result(timeout=300)

    def _reset(self):
        for closer in (lambda: self.context and self.context.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self.context, self.page, self._pw = None, None, None
        self.items, self.frames = {}, {}
        self.next_id, self.fresh = 1, True

    # ----- lifecycle (browser thread) -----
    def _ensure(self):
        if self.context is not None:
            try:
                pages = [p for p in self.context.pages if not p.is_closed()]
                if self.page is None or self.page.is_closed():
                    self.page = pages[-1] if pages else self.context.new_page()
                return
            except Exception:
                self.context = None
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        options = dict(user_data_dir=str(self.profile_dir), headless=settings.browser_headless, accept_downloads=True,
                       ignore_default_args=["--enable-automation"],
                       args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
        if settings.browser_headless:
            options["viewport"] = {"width": 1366, "height": 900}
        else:
            options["no_viewport"] = True
        errors = []
        for channel in dict.fromkeys([settings.browser_channel, "chrome", "msedge", None]):
            try:
                kwargs = dict(options, channel=channel) if channel else options
                self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as exc:
                errors.append(f"{channel}: {str(exc).splitlines()[0][:150]}")
        if self.context is None:
            raise RuntimeError("Could not start Chrome/Edge: " + " | ".join(errors))
        self.context.on("page", self._on_page)
        self.context.on("close", lambda *_: setattr(self, "context", None))
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._hook(self.page)

    def _hook(self, page):
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)

    def _on_page(self, page):
        self._hook(page)
        self.page = page
        self.events.append("A new tab opened and is now active.")

    def _on_dialog(self, dialog):
        message = dialog.message[:200]
        if dialog.type == "confirm" and not self.accept_confirms:
            dialog.dismiss()
            self.events.append(f"Dismissed a confirmation popup: '{message}'")
        else:
            dialog.accept()
            self.events.append(f"Accepted a {dialog.type} popup: '{message}'")

    def _on_download(self, download):
        folder = settings.workspace / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / download.suggested_filename
        download.save_as(str(target))
        self.events.append(f"Downloaded file to {target}")

    def _settle(self, ms: int = 700):
        page = self.page
        for state, timeout in (("domcontentloaded", 10000), ("networkidle", 3000)):
            try:
                page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass
        page.wait_for_timeout(ms)

    def shutdown(self):
        def _close():
            try:
                if self.context is not None:
                    self.context.close()
            finally:
                self.context = None
                if self._pw is not None:
                    self._pw.stop()
                    self._pw = None
        try:
            self.call(_close)
        except Exception:
            pass

    # ----- page reading (browser thread) -----
    def _snapshot(self, max_items: int = 90, text_chars: int = 1500, filter_text: str | None = None) -> str:
        page = self.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        items, frames = [], {}
        reset = self.fresh
        for index, frame in enumerate(page.frames[:10]):
            if frame.is_detached():
                continue
            if index > 0:
                try:
                    box = frame.frame_element().bounding_box()
                    if not box or box["width"] < 80 or box["height"] < 40:
                        continue
                except Exception:
                    continue
            try:
                found = frame.evaluate(SNAPSHOT_JS, {"next": self.next_id, "reset": reset, "max": max(0, 400 - len(items))})
            except Exception:
                continue
            found, self.next_id = snap_result(found, self.next_id)
            for it in found:
                frames[it["id"]] = frame
            items.extend(found)
        self.fresh = False
        self.items = {it["id"]: it for it in items}
        self.frames = frames
        self.url = page.url
        try:
            self.title = page.title()
        except Exception:
            self.title = ""
        try:
            text = page.main_frame.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            text = ""
        events, self.events = self.events[-5:], []
        return format_snapshot(page.url, self.title, items, events, text, max_items, text_chars, filter_text)

    def _pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000)

    def current_url(self) -> str:
        return self.page.url if self.page is not None else ""

    def all_text(self) -> str:
        if self.page is None or self.page.is_closed():
            return ""
        texts = []
        for frame in self.page.frames[:6]:
            try:
                t = frame.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                continue
            if t and (len(t.strip()) > 40 or frame == self.page.main_frame):
                texts.append(t)
        return "\n\n".join(texts)

    def form_check(self, element_id) -> dict:
        self._ensure()
        element_id = int(element_id)
        frame = self.frames.get(element_id) or self.page.main_frame
        return frame.evaluate(PAGE_CALL_JS, {"fn": "formCheck", "args": {"id": element_id}}) or {}

    def form_values(self, element_id) -> list[dict]:
        self._ensure()
        element_id = int(element_id)
        frame = self.frames.get(element_id) or self.page.main_frame
        out = frame.evaluate(PAGE_CALL_JS, {"fn": "formValues", "args": {"id": element_id}})
        return out if isinstance(out, list) else []

    def _locator(self, element_id=None, text: str | None = None):
        page = self.page
        if element_id is not None:
            element_id = int(element_id)
            frame = self.frames.get(element_id) or page.main_frame
            loc = frame.locator(f'[data-jid="{element_id}"]')
            if loc.count() == 0:
                raise LookupError(f"Element [{element_id}] is gone (page changed). Call browser_snapshot for fresh ids.")
            return loc.first, self.items.get(element_id)
        if text:
            found = self.find_item(None, text)
            if found:
                return self._locator(found["id"])
            return page.get_by_text(text, exact=False).first, None
        raise ValueError("Give element_id (from browser_snapshot) or text.")

    def _pick_suggestion(self, element_id: int, value: str) -> str:
        """After typing into a combobox: click the matching suggestion with a real mouse click.
        Returns the picked text, or '' (self.last_options then holds what the list showed)."""
        frame = self.frames.get(int(element_id)) or self.page.main_frame
        self.last_options = []
        for attempt in range(12):
            self.page.wait_for_timeout(250)
            try:
                found = frame.evaluate(PAGE_CALL_JS, {"fn": "markOption", "args": {"value": str(value)}}) or {}
            except Exception:
                return ""
            if found.get("text"):
                try:
                    frame.locator("[data-karya-opt]").first.click(timeout=3000)
                    return found["text"]
                except Exception:
                    return ""
            self.last_options = found.get("options") or self.last_options
            if found.get("count") and attempt > 6:
                break
        return ""

    def _no_match_note(self, value: str) -> str:
        shown = " | ".join(getattr(self, "last_options", None) or []) or "none"
        return (f" (no suggestion matches \"{value}\"; the list shows: {shown}. Type an option exactly, or ask_user if you "
                "don't know the user's answer)")

    # ----- actions (browser thread) -----
    def open(self, url: str, new_tab: bool = False):
        self._ensure()
        if not re.match(r"^[a-z]+://", url, re.I) and url != "about:blank":
            url = "https://" + url
        if new_tab and len([p for p in self.context.pages if not p.is_closed()]) < 5:
            self.page = self.context.new_page()
            self._hook(self.page)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            self.events.append(f"Navigation problem: {str(exc).splitlines()[0][:150]}")
        self._settle(900)
        return self._snapshot()

    def snapshot(self, filter_text=None, max_items=100):
        self._ensure()
        return self._snapshot(max_items=max_items, text_chars=2000, filter_text=filter_text)

    def click(self, element_id=None, text=None, double=False):
        self._ensure()
        loc, item = self._locator(element_id, text)
        label = (item or {}).get("label") or text or ""
        submit = is_submit_click(item, label, self.url)
        before = (self.page.url, self.all_text()) if submit else None
        self.accept_confirms = classify_click(item, label, self.url) != SAFE
        try:
            try:
                loc.dblclick(timeout=8000) if double else loc.click(timeout=8000)
            except Exception:
                try:
                    loc.click(timeout=4000, force=True)
                except Exception:
                    loc.evaluate("e => e.click()")
            self._settle()
            verdict = self.await_verdict(before) if submit else ""
        finally:
            self.accept_confirms = False
        return f"Clicked \"{label}\".\n" + (verdict + "\n" if verdict else "") + self._snapshot(max_items=80, text_chars=1200)

    def type_text(self, element_id=None, text="", clear=True, submit=False, label=None):
        self._ensure()
        loc, item = self._locator(element_id, label)
        loc.scroll_into_view_if_needed(timeout=5000)
        page = self.page
        picked = ""
        if item and item.get("editable") or (item and item.get("role") == "textbox" and item.get("tag") not in ("input", "textarea")):
            loc.click()
            if clear:
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
            for n, line in enumerate(text.split("\n")):
                if n:
                    page.keyboard.press("Shift+Enter")
                if line:
                    page.keyboard.insert_text(line)
            try:
                if text.strip() and text.strip().split("\n")[0][:15] not in loc.inner_text(timeout=3000):
                    loc.press_sequentially(text, delay=8)
            except Exception:
                pass
        else:
            if clear:
                loc.fill(text, timeout=8000)
            else:
                loc.press_sequentially(text, delay=5)
            try:
                if text and loc.input_value(timeout=2000) != text and clear:
                    loc.fill("")
                    loc.press_sequentially(text, delay=8)
            except Exception:
                pass
            if wants_pick(item) and not submit:
                picked = self._pick_suggestion(item["id"], text)
        self.last_typed = item or {"label": label or ""}
        if submit:
            loc.press("Enter")
            self._settle()
            return "Typed and pressed Enter.\n" + self._snapshot(max_items=80, text_chars=1200)
        listed = item and wants_pick(item) and (item.get("role") == "combobox" or getattr(self, "last_options", None))
        return f"Typed {len(text)} chars into \"{(item or {}).get('label', label or '')}\"" + (
            f" and picked \"{picked}\"." if picked else (self._no_match_note(text) + "." if listed else "."))

    def fill_many(self, fields: dict):
        self._ensure()
        done, problems = [], []
        for raw_id, value in fields.items():
            label = f"[{raw_id}]"
            try:
                element_id = int(str(raw_id).strip("[] "))
                loc, item = self._locator(element_id)
                item = item or {}
                label = item.get("label") or f"[{element_id}]"
                op, val = field_action(item, value)
                note = ""
                if op == "select":
                    try:
                        loc.select_option(label=str(val), timeout=5000)
                    except Exception:
                        loc.select_option(value=str(val), timeout=5000)
                elif op == "check":
                    if item.get("tag") == "input":
                        loc.set_checked(bool(val), timeout=5000, force=True)
                    elif bool(item.get("checked")) != bool(val):
                        loc.click(timeout=5000)
                elif op == "upload":
                    loc.set_input_files(str(val))
                elif op == "click":
                    loc.click(timeout=5000)
                elif op == "type":
                    loc.click(timeout=5000)
                    self.page.keyboard.press("Control+A")
                    self.page.keyboard.insert_text(str(val))
                elif op == "fill":
                    loc.fill(str(val), timeout=8000)
                    if wants_pick(item):
                        chosen = self._pick_suggestion(element_id, str(val))
                        if chosen:
                            note = f" -> picked \"{chosen}\""
                        elif item.get("role") == "combobox" or self.last_options:
                            note = self._no_match_note(str(val))
                done.append(label + note)
            except Exception as exc:
                problems.append(f"[{raw_id}] {label}: {str(exc).splitlines()[0][:150]}")
        self.page.wait_for_timeout(400)
        text = f"Filled {len(done)} field(s): {', '.join(done)}" + (f"\nProblems: {'; '.join(problems)}" if problems else "")
        return text + "\n" + self._snapshot(max_items=80, text_chars=600)

    def type_secret(self, element_id, site, field="password", submit=False):
        from .. import vault
        self._ensure()
        creds = vault.get_secret(site)
        if not creds:
            return f"ERROR: no saved {field} for {site}. Use request_credentials."
        username, password = creds
        value = password if field == "password" else username
        loc, item = self._locator(element_id)
        loc.fill(value, timeout=8000)
        if submit:
            loc.press("Enter")
            self._settle()
            return f"Entered the saved {field} for {site} and pressed Enter.\n" + self._snapshot(max_items=80, text_chars=1000)
        return f"Entered the saved {field} for {site} into \"{(item or {}).get('label', element_id)}\"."

    def select(self, element_id, option):
        self._ensure()
        loc, item = self._locator(element_id)
        if item and item.get("tag") == "select":
            try:
                loc.select_option(label=option, timeout=5000)
            except Exception:
                loc.select_option(value=option, timeout=5000)
        else:  # custom dropdown: open it, then click the option
            loc.click(timeout=8000)
            self.page.wait_for_timeout(500)
            target = self.page.get_by_role("option", name=option).first
            if target.count() == 0:
                target = self.page.get_by_text(option, exact=True).first
            target.click(timeout=8000)
        self._settle(400)
        return f"Selected \"{option}\".\n" + self._snapshot(max_items=70, text_chars=800)

    def set_checked(self, element_id, checked=True):
        self._ensure()
        loc, item = self._locator(element_id)
        if item and item.get("tag") == "input":
            loc.set_checked(checked, timeout=8000, force=True)
        elif bool((item or {}).get("checked")) != checked:
            loc.click(timeout=8000)
        return f"Set [{element_id}] to {'checked' if checked else 'unchecked'}."

    def upload(self, element_id, file_path):
        self._ensure()
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        loc, item = self._locator(element_id)
        if item and item.get("tag") == "input" and item.get("type") == "file":
            loc.set_input_files(str(path))
        else:
            try:
                with self.page.expect_file_chooser(timeout=8000) as chooser:
                    loc.click()
                chooser.value.set_files(str(path))
            except Exception:
                self.page.locator("input[type=file]").first.set_input_files(str(path))
        self._settle(800)
        return f"Uploaded {path.name}.\n" + self._snapshot(max_items=70, text_chars=800)

    def press(self, key):
        self._ensure()
        self.page.keyboard.press(key)
        self._settle(500)
        return f"Pressed {key}.\n" + self._snapshot(max_items=70, text_chars=1000)

    def scroll(self, direction="down", pages=1.0):
        self._ensure()
        page = self.page
        if direction == "top":
            page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        else:
            height = page.evaluate("() => window.innerHeight") or 800
            page.mouse.wheel(0, (1 if direction == "down" else -1) * height * 0.85 * float(pages))
        page.wait_for_timeout(700)
        return self._snapshot(max_items=80, text_chars=1200)

    def back(self):
        self._ensure()
        self.page.go_back(wait_until="domcontentloaded", timeout=20000)
        self._settle(500)
        return self._snapshot(max_items=80, text_chars=1200)

    def read_text(self, max_chars=8000):
        self._ensure()
        text = re.sub(r"\n\s*\n+", "\n", self.all_text()).strip()
        return f"URL: {self.page.url}\n{text[:max_chars]}" + (f"\n...[{len(text) - max_chars} more chars]" if len(text) > max_chars else "")

    def screenshot(self, full_page=False):
        self._ensure()
        folder = settings.workspace / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"page_{time.strftime('%Y%m%d_%H%M%S')}.png"
        self.page.screenshot(path=str(target), full_page=full_page)
        return f"Saved screenshot: {target}"

    def tabs(self, action="list", index=None, url=None):
        self._ensure()
        pages = [p for p in self.context.pages if not p.is_closed()]
        if action == "new":
            return self.open(url or "about:blank", new_tab=True)
        if action in ("switch", "close"):
            if index is None or not (0 <= int(index) < len(pages)):
                return f"ERROR: tab index must be 0..{len(pages) - 1}"
            target = pages[int(index)]
            if action == "close":
                target.close()
                pages = [p for p in self.context.pages if not p.is_closed()]
                self.page = pages[-1] if pages else self.context.new_page()
            else:
                self.page = target
                target.bring_to_front()
            return self._snapshot(max_items=80, text_chars=800)
        return [{"index": i, "title": p.title()[:80], "url": p.url[:150], "active": p == self.page} for i, p in enumerate(pages)]

    def wait(self, seconds=3):
        self._ensure()
        self.page.wait_for_timeout(max(0.5, min(float(seconds), 30)) * 1000)
        return self._snapshot(max_items=80, text_chars=1200)

    # ----- positions: canvas, maps, game boards (real mouse) -----
    def _point(self, element_id, x, y) -> tuple[float, float]:
        if element_id is None:
            return float(x), float(y)
        loc, _ = self._locator(element_id)
        loc.scroll_into_view_if_needed(timeout=5000)
        box = loc.bounding_box()
        if not box:
            raise LookupError(f"Element [{element_id}] has no size on the page")
        px, py = box["x"] + float(x) * box["width"], box["y"] + float(y) * box["height"]
        frame = self.frames.get(int(element_id)) or self.page.main_frame
        if frame == self.page.main_frame:  # (inside iframes the page's own coordinates differ)
            cover = frame.evaluate(PAGE_CALL_JS, {"fn": "coverAt", "args": {"id": int(element_id), "points": [[px, py]]}})
            if cover:
                raise LookupError(f"something on the page is covering that area (\"{cover}\"), so the click would hit it "
                                  "instead. Close it first (browser_snapshot shows its buttons, or browser_press Escape).")
        return px, py

    def click_at(self, element_id=None, x=0.5, y=0.5, double=False):
        self._ensure()
        px, py = self._point(element_id, x, y)
        self.page.mouse.click(px, py, click_count=2 if double else 1)
        self._settle(400)
        return f"Clicked at ({int(px)}, {int(py)}).\n" + self._snapshot(max_items=80, text_chars=800)

    def drag(self, element_id=None, x=0.0, y=0.0, to_x=0.0, to_y=0.0, steps=12):
        self._ensure()
        x1, y1 = self._point(element_id, x, y)
        x2, y2 = self._point(element_id, to_x, to_y)
        mouse = self.page.mouse
        mouse.move(x1, y1)
        mouse.down()
        mouse.move(x2, y2, steps=max(4, min(40, int(steps or 12))))
        mouse.up()
        self._settle(400)
        return f"Dragged from ({int(x1)}, {int(y1)}) to ({int(x2)}, {int(y2)}).\n" + self._snapshot(max_items=80, text_chars=800)

    def move_piece(self, element_id=None, from_square="", to_square="", promotion="q"):
        self._ensure()
        frame = self.page.main_frame
        call = lambda fn, args: frame.evaluate(PAGE_CALL_JS, {"fn": fn, "args": args})  # noqa: E731
        args = {"id": int(element_id) if element_id is not None else None}
        plan = call("planMove", {**args, "from": str(from_square), "to": str(to_square), "promotion": promotion or "q"})
        if not plan or plan.get("ok") is False:
            return f"NOT MOVED: {(plan or {}).get('error') or 'there is no chess board Karya can read on this page'}"
        mouse = self.page.mouse

        def check():
            self.page.wait_for_timeout(450)
            state = call("checkMove", plan) or {}
            if state.get("promotion"):
                mouse.click(*state["promotion"])
                self.page.wait_for_timeout(350)
                state = call("checkMove", plan) or {}
            return state

        (x1, y1), (x2, y2) = plan["from"], plan["to"]
        mouse.click(x1, y1)
        self.page.wait_for_timeout(150)
        mouse.click(x2, y2)
        state = check()
        if not state.get("moved"):
            mouse.move(x1, y1)
            mouse.down()
            mouse.move(x2, y2, steps=12)
            mouse.up()
            state = check()
        board = call("board", args) or {}
        if not state.get("moved"):
            return (f"NOT MOVED: {plan['from_sq']}-{plan['to_sq']} didn't happen (not your turn, an illegal move, or the "
                    f"game isn't running). Position: {_pieces_text(board.get('pieces') or {})}")
        return f"Moved {plan['from_sq']}-{plan['to_sq']}. Position now: {_pieces_text(board.get('pieces') or {})}"

    def close(self):
        if self.context is not None:
            self.context.close()
        self.context = None
        return "Browser closed."


# ================================================================ the user's own Chrome (Karya Browser Link)
class ExtensionSession(_Common):
    """Same actions as BrowserSession, run by the extension in Karya's own tab of the user's Chrome."""

    kind = "chrome"
    MAX_UPLOAD = 15 * 1024 * 1024

    def __init__(self) -> None:
        self.items: dict[int, dict] = {}
        self.frames: dict[int, int] = {}
        self.url = ""
        self.title = ""
        self.last_typed: dict | None = None
        self.next_id = 1
        self.fresh = True

    def call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ExtensionUnavailable as exc:
            raise RuntimeError(str(exc)) from None

    def _req(self, method: str, timeout: float = 45.0, **params) -> dict:
        return link.request(method, params, timeout=timeout)

    def _act(self, element_id, op: str, **args) -> dict:
        element_id = int(element_id)
        tag = (self.items.get(element_id) or {}).get("tag")
        if tag:
            args.setdefault("expect", tag)
        result = self._req("act", frame=self.frames.get(element_id, 0), op=op, args={"id": element_id, **args}) or {}
        if isinstance(result, dict) and result.get("ok") is False:
            raise LookupError(result.get("error") or f"{op} failed")
        return result if isinstance(result, dict) else {}

    def _resolve(self, element_id=None, text: str | None = None) -> int:
        if element_id is not None:
            return int(element_id)
        found = self.find_item(None, text)
        if found:
            return found["id"]
        if text:
            raise LookupError(f"No element labelled \"{text}\" in the last snapshot. Call browser_snapshot first.")
        raise ValueError("Give element_id (from browser_snapshot) or text.")

    def _settle(self, ms: int = 600) -> None:
        try:
            info = self._req("settle", timeout=20, ms=ms)
            self.url = info.get("url") or self.url
        except RuntimeError:
            pass

    def _snapshot(self, max_items: int = 90, text_chars: int = 1500, filter_text: str | None = None) -> str:
        snap = self._req("snapshot", timeout=40, next=self.next_id, reset=self.fresh, max=400, text_chars=text_chars)
        items, self.next_id = snap_result({"items": snap.get("items"), "next": snap.get("next")}, self.next_id)
        self.fresh = False
        self.items = {it["id"]: it for it in items}
        self.frames = {it["id"]: int(it.get("frame") or 0) for it in items}
        self.url, self.title = snap.get("url") or "", snap.get("title") or ""
        events, link.events[:] = link.events[-5:], []
        return format_snapshot(self.url, self.title, items, events, snap.get("text") or "", max_items, text_chars,
                               filter_text, where="(Karya's tab in your Chrome)")

    def _pause(self, seconds: float) -> None:
        time.sleep(seconds)

    def current_url(self) -> str:
        return (self._req("settle", timeout=20, ms=0) or {}).get("url") or self.url

    def all_text(self) -> str:
        if not self.url:
            return ""
        return (self._req("text", max_chars=60000) or {}).get("text") or ""

    def form_check(self, element_id) -> dict:
        element_id = int(element_id)
        return self._req("act", frame=self.frames.get(element_id, 0), op="formcheck", args={"id": element_id}) or {}

    def form_values(self, element_id) -> list[dict]:
        element_id = int(element_id)
        out = self._req("act", frame=self.frames.get(element_id, 0), op="formvalues", args={"id": element_id})
        return out if isinstance(out, list) else []

    # ----- actions -----
    def open(self, url: str, new_tab: bool = False):
        if not re.match(r"^[a-z]+://", url, re.I) and url != "about:blank":
            url = "https://" + url
        self._req("open", timeout=60, url=url, new_tab=bool(new_tab))
        if url == "about:blank":
            return "Opened an empty tab in your Chrome."
        self._settle(700)
        return self._snapshot()

    def snapshot(self, filter_text=None, max_items=100):
        return self._snapshot(max_items=max_items, text_chars=2000, filter_text=filter_text)

    def click(self, element_id=None, text=None, double=False):
        eid = self._resolve(element_id, text)
        item = self.items.get(eid)
        label = (item or {}).get("label") or text or ""
        submit = is_submit_click(item, label, self.url)
        before = (self.url, self.all_text()) if submit else None
        try:
            result = self._act(eid, "click", double=bool(double))
        except RuntimeError as exc:
            if not _NAVIGATED.search(str(exc)):
                raise
            result = {"ok": True}  # the click started loading a new page before the script could answer
        self._settle(700)
        verdict = self.await_verdict(before) if submit else ""
        note = "The link opened in a new Karya tab (now active).\n" if result.get("new_tab") else ""
        return f"Clicked \"{label}\".\n" + note + (verdict + "\n" if verdict else "") + self._snapshot(max_items=80, text_chars=1200)

    def type_text(self, element_id=None, text="", clear=True, submit=False, label=None):
        eid = self._resolve(element_id, label)
        item = self.items.get(eid) or {"id": eid, "label": label or ""}
        result = self._act(eid, "set", value=text, clear=clear, pick=not is_search_field(item) and not submit, keep_focus=submit)
        self.last_typed = item
        if submit:
            self._act(eid, "press", key="Enter")
            self._settle(700)
            return "Typed and pressed Enter.\n" + self._snapshot(max_items=80, text_chars=1200)
        extra = ""
        if result.get("picked"):
            extra = f" and picked \"{result['picked']}\""
        elif result.get("combo") and result.get("options"):
            extra = "; no suggestion matched. Suggestions: " + " | ".join(result["options"])
        return f"Typed {len(text)} chars into \"{item.get('label', '')}\"{extra}."

    def fill_many(self, fields: dict):
        done, problems = [], []
        for raw_id, value in fields.items():
            label = f"[{raw_id}]"
            try:
                eid = int(str(raw_id).strip("[] "))
                item = self.items.get(eid) or {}
                label = item.get("label") or f"[{eid}]"
                op, val = field_action(item, value)
                note = ""
                if op == "select":
                    note = f" -> \"{self._act(eid, 'select', option=str(val)).get('picked', val)}\""
                elif op == "check":
                    self._act(eid, "check", checked=bool(val))
                elif op == "upload":
                    self._send_file(eid, str(val))
                elif op == "click":
                    self._act(eid, "click")
                elif op in ("type", "fill"):
                    result = self._act(eid, "set", value=str(val), pick=not is_search_field(item))
                    if result.get("combo"):
                        if result.get("picked"):
                            note = f" -> picked \"{result['picked']}\""
                        else:
                            shown = " | ".join(result.get("options") or []) or "none"
                            note = (f" (no suggestion matches \"{val}\"; the list shows: {shown}. Type an option exactly, "
                                    "or ask_user if you don't know the user's answer)")
                done.append(label + note)
            except Exception as exc:  # noqa: BLE001 - report per field, keep filling the rest
                problems.append(f"[{raw_id}] {label}: {str(exc).splitlines()[0][:150]}")
        time.sleep(0.4)
        text = f"Filled {len(done)} field(s): {', '.join(done)}" + (f"\nProblems: {'; '.join(problems)}" if problems else "")
        return text + "\n" + self._snapshot(max_items=80, text_chars=600)

    def type_secret(self, element_id, site, field="password", submit=False):
        from .. import vault
        creds = vault.get_secret(site)
        if not creds:
            return f"ERROR: no saved {field} for {site}. Use request_credentials."
        username, password = creds
        eid = int(element_id)
        self._act(eid, "set", value=password if field == "password" else username, secret=True, pick=False, keep_focus=submit)
        item = self.items.get(eid) or {}
        if submit:
            self._act(eid, "press", key="Enter")
            self._settle(800)
            return f"Entered the saved {field} for {site} and pressed Enter.\n" + self._snapshot(max_items=80, text_chars=1000)
        return f"Entered the saved {field} for {site} into \"{item.get('label', eid)}\"."

    def select(self, element_id, option):
        result = self._act(int(element_id), "select", option=str(option))
        self._settle(400)
        return f"Selected \"{result.get('picked', option)}\".\n" + self._snapshot(max_items=70, text_chars=800)

    def set_checked(self, element_id, checked=True):
        self._act(int(element_id), "check", checked=bool(checked))
        return f"Set [{element_id}] to {'checked' if checked else 'unchecked'}."

    def _send_file(self, element_id: int, file_path: str) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        if path.stat().st_size > self.MAX_UPLOAD:
            raise ValueError(f"{path.name} is bigger than 15 MB")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._act(element_id, "upload", b64=base64.b64encode(path.read_bytes()).decode("ascii"), name=path.name, mime=mime)
        return path.name

    def upload(self, element_id, file_path):
        name = self._send_file(int(element_id), file_path)
        self._settle(800)
        return f"Uploaded {name}.\n" + self._snapshot(max_items=70, text_chars=800)

    def press(self, key):
        frame = self.frames.get(int((self.last_typed or {}).get("id") or 0), 0)
        try:
            self._req("act", frame=frame, op="press", args={"key": key})
        except RuntimeError as exc:
            if not _NAVIGATED.search(str(exc)):
                raise
        self._settle(500)
        return f"Pressed {key}.\n" + self._snapshot(max_items=70, text_chars=1000)

    def scroll(self, direction="down", pages=1.0):
        self._req("act", frame=0, op="scroll", args={"direction": direction, "pages": float(pages or 1)})
        time.sleep(0.7)
        return self._snapshot(max_items=80, text_chars=1200)

    def back(self):
        self._req("back", timeout=20)
        self._settle(500)
        return self._snapshot(max_items=80, text_chars=1200)

    def read_text(self, max_chars=8000):
        text = re.sub(r"\n\s*\n+", "\n", self.all_text()).strip()
        return f"URL: {self.url}\n{text[:max_chars]}" + (f"\n...[{len(text) - max_chars} more chars]" if len(text) > max_chars else "")

    def screenshot(self, full_page=False):
        png = (self._req("screenshot", timeout=20) or {}).get("png")
        if not png:
            return "ERROR: Chrome didn't return a screenshot."
        folder = settings.workspace / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"page_{time.strftime('%Y%m%d_%H%M%S')}.png"
        target.write_bytes(base64.b64decode(png))
        return f"Saved screenshot: {target}" + (" (visible part of the page only)" if full_page else "")

    def tabs(self, action="list", index=None, url=None):
        if action == "new":
            return self.open(url or "about:blank", new_tab=True)
        if action in ("switch", "close"):
            self._req("tabs", action=action, index=None if index is None else int(index))
            if action == "close" and not (self._req("tabs", action="list") or {}).get("tabs"):
                return "Closed the tab. Karya has no tabs open now."
            return self._snapshot(max_items=80, text_chars=800)
        return (self._req("tabs", action="list") or {}).get("tabs") or []

    def wait(self, seconds=3):
        time.sleep(max(0.5, min(float(seconds), 30)))
        return self._snapshot(max_items=80, text_chars=1200)

    # ----- positions: canvas, maps, game boards -----
    def _point_args(self, element_id, **values) -> tuple[int, dict]:
        args = {k: float(v) for k, v in values.items() if v is not None}
        if element_id is None:
            return 0, args
        return self.frames.get(int(element_id), 0), {**args, "id": int(element_id)}

    def click_at(self, element_id=None, x=0.5, y=0.5, double=False):
        frame, args = self._point_args(element_id, x=x, y=y)
        result = self._req("act", frame=frame, op="click_at", args={**args, "double": bool(double)}) or {}
        if result.get("ok") is False:
            raise LookupError(result.get("error") or "click failed")
        self._settle(500)
        where = f" on \"{result['target']}\"" if result.get("target") else ""
        return f"Clicked at {result.get('at')}{where}.\n" + self._snapshot(max_items=80, text_chars=800)

    def drag(self, element_id=None, x=0.0, y=0.0, to_x=0.0, to_y=0.0, steps=12):
        frame, args = self._point_args(element_id, x=x, y=y, to_x=to_x, to_y=to_y)
        result = self._req("act", frame=frame, op="drag", args={**args, "steps": int(steps or 12)}) or {}
        if result.get("ok") is False:
            raise LookupError(result.get("error") or "drag failed")
        self._settle(500)
        return f"Dragged from {result.get('from')} to {result.get('to')}.\n" + self._snapshot(max_items=80, text_chars=800)

    def move_piece(self, element_id=None, from_square="", to_square="", promotion="q"):
        frame, args = self._point_args(element_id)
        result = self._req("act", frame=frame, op="move_piece", timeout=30, args={
            **args, "from": from_square, "to": to_square, "promotion": promotion or "q"}) or {}
        if result.get("ok") is False:
            return f"NOT MOVED: {result.get('error')}"
        board = self._req("act", frame=frame, op="board", args=args) or {}
        return f"Moved {result.get('moved')}. Position now: {_pieces_text(board.get('pieces') or {})}"

    def close(self):
        self._req("close_tabs")
        self.items, self.frames, self.url = {}, {}, ""
        return "Closed Karya's tabs in your Chrome."


session = BrowserSession()        # Karya's own window
ext_session = ExtensionSession()  # the user's Chrome


def _current():
    """The backend the next action will use (no waiting; for risk checks)."""
    if settings.browser_mode != "karya" and link.connected:
        return ext_session
    return session


def _use():
    """The backend to act with. Never switches to a different browser in the middle of a task without saying so."""
    mode = settings.browser_mode
    if mode != "karya":
        if link.connected:
            return ext_session
        if mode == "chrome" or time.time() - link.last_used < 600:
            if link.wait_connected(6):
                return ext_session
            raise RuntimeError("Karya Browser Link isn't connected, so Karya can't use your Chrome right now. Open Chrome "
                               "(with the Karya Browser Link extension) and check Setup shows 'Your Chrome: connected'."
                               + ("" if mode == "chrome" else " Or choose \"Karya's own window\" as the browser in Setup."))
    return session


def _run(method: str, *args):
    backend = None
    try:
        backend = _use()
        return backend.call(getattr(backend, method), *args)
    except LookupError as exc:
        text = str(exc).splitlines()[0]
        if backend is None or not re.search(r"\bgone\b|isn'?t the|no element labelled|not in the last snapshot", text, re.I):
            return f"ERROR: {text}"
        try:  # the page changed: hand back the fresh page so the next step can act on it straight away
            snap = backend.call(backend.snapshot, None, 90)
        except Exception:  # noqa: BLE001
            snap = ""
        reason = re.split(r"\s*(?:Take a new|Call browser_snapshot)", text)[0]
        return f"NOT DONE: {reason} Here is the page now - use these ids:\n{snap}" if snap else f"NOT DONE: {text}"
    except RuntimeError as exc:
        return f"ERROR: {str(exc).splitlines()[0][:400]}"


def _host() -> str:
    return _current().host


def _item(args) -> dict | None:
    return _current().find_item(args.get("element_id"), args.get("text") if args.get("element_id") is None else None)


_CHOICE_ROLES = ("button", "radio", "option", "checkbox", "switch", "menuitemradio", "tab")


def _is_choice(item: dict) -> bool:
    return item.get("tag") in ("button", "a") or item.get("role") in _CHOICE_ROLES or item.get("type") in ("radio", "checkbox")


def answer_problem(item: dict | None, value) -> str | None:
    """Why this value may not go into this field (a guess at something only the user knows), or None."""
    if not item:
        return None
    from .. import answers
    if _is_choice(item):  # a Yes/No button, radio or checkbox: the claim is the option's own label
        question = item.get("question") or ""
        if not question or (value is not None and str(value).strip().lower() in ("false", "0", "off", "unchecked")):
            return None
        return answers.check(question, item.get("label") or "")
    for text in dict.fromkeys(t for t in (item.get("label"), item.get("question")) if t):
        problem = answers.check(text, value)  # either text may carry the real question
        if problem:
            return problem
    return None


def _needs_answers(problems: list[str], verb: str) -> str:
    return (f"{verb} (Karya doesn't type guesses): " + "; ".join(problems) + ". Ask the user with ask_user (all such "
            "questions of this form in one call, worded as on the form), then fill in exactly what they answer.")


def _form_answers(backend, element_id) -> list[dict]:
    try:
        return backend.call(backend.form_values, element_id) or []
    except Exception:  # noqa: BLE001 - only used to show the user what is on the form
        return []


def _click_risk(args):
    item = _item(args)
    label = (item or {}).get("label") or args.get("text") or f"element {args.get('element_id')}"
    if item is None and args.get("element_id") is not None and not args.get("text"):
        return CONFIRM, f"Click element [{args.get('element_id')}] on {_host()} (not in the last snapshot)"
    backend = _current()
    level = classify_click(item, label, backend.url)
    summary = f"Click \"{label}\" on {_host()}"
    if level == CRITICAL and item and is_submit_click(item, label, backend.url):
        from .. import apply_queue
        from . import jobs as jobs_mod
        job = apply_queue.find_by_url(backend.url) or apply_queue.find_by_url(backend.url, jobs=list(jobs_mod.cached_jobs().values()))
        if job:
            summary = f"Submit your application: {job.get('title', '')} at {job.get('company', '')}\n" + summary
        values = _form_answers(backend, item["id"])
        if values:  # the user approves knowing exactly what goes out
            summary += "\n\nWhat this form will send (check before you approve):\n" + "\n".join(
                f"- {v.get('q', '')[:70]}: {v.get('v', '')[:140]}" for v in values[:30])
    if args.get("confirm_empty"):
        summary += "\nWARNING: Karya found required fields that look empty. Submitting will probably fail."
    return level, summary


def _not_picked(url: str) -> str | None:
    """While the user's pick list is active, a job from the shortlist that they did NOT pick is never submitted."""
    from .. import apply_queue
    from . import jobs as jobs_mod
    if not apply_queue.load() or apply_queue.age_seconds() > apply_queue.MAX_AGE_SECONDS:
        return None
    if apply_queue.find_by_url(url):
        return None
    job = apply_queue.find_by_url(url, jobs=list(jobs_mod.cached_jobs().values()))
    if not job:
        return None  # not from the shortlist (e.g. a link the user gave): their own request
    return (f'NOT CLICKED (nothing was submitted): {job["id"]} "{job["title"]}" at {job["company"]} is not on the list '
            "of jobs the user picked. If they asked for it (e.g. \"apply to all of them\"), show it with choose_jobs "
            "(pass its id; it comes pre-ticked) so it's on their list, then submit.")


def _type_precheck(args) -> str | None:
    backend = _current()
    target = backend.find_item(args.get("element_id"), args.get("label") if args.get("element_id") is None else None)
    problem = answer_problem(target, args.get("text"))
    if problem:
        return _needs_answers([problem], "NOT TYPED")
    host = urlparse(backend.url).netloc.lower()
    if args.get("submit") and not is_search_field(target) and host and _FORM_HOSTS.search(host):
        return ("NOT TYPED: don't press Enter inside an application form (it can submit it half-filled). Type without "
                "submit, fill the other fields, then click the form's Submit button.")
    return None


def _select_precheck(args) -> str | None:
    problem = answer_problem(_current().find_item(args.get("element_id")), args.get("option"))
    return _needs_answers([problem], "NOT SELECTED") if problem else None


def _check_precheck(args) -> str | None:
    item = _current().find_item(args.get("element_id"))
    captcha = captcha_problem(item)
    if captcha:
        return captcha
    if args.get("checked") is False:
        return None
    problem = answer_problem(item, True)
    return _needs_answers([problem], "NOT TICKED") if problem else None


def pre_approved_submit(args: dict) -> str | None:
    """The user chose "submit the jobs I pick without asking": allowed only for a Submit on a picked job that is
    still to do, at most twice per job, and never again after an unclear result. Returns the note to show, or None."""
    from .. import apply_queue
    if not apply_queue.auto_submit_on():
        return None
    backend = _current()
    item = _item(args)
    if not item or args.get("confirm_empty") or not is_submit_click(item, item.get("label") or "", backend.url):
        return None
    job = apply_queue.find_by_url(backend.url)
    if not job or job.get("status") != "pending" or int(job.get("auto_tries") or 0) >= 2 \
            or job.get("last_result") == "unconfirmed":
        return None
    apply_queue.bump_try(job["id"])
    values = _form_answers(backend, item["id"])
    sent = "; ".join(f"{v.get('q', '')[:50]}: {v.get('v', '')[:80]}" for v in values[:20])
    return (f"Submitting {job['title']} at {job['company']} without asking (you pre-approved the jobs you picked)."
            + (f" Sending: {sent}" if sent else ""))


def denied_note(args: dict) -> str:
    """The user said no to a Submit: that picked job is skipped (Karya won't ask again) and the AI moves on."""
    from .. import apply_queue
    backend = _current()
    item = _item(args)
    if not item or not is_submit_click(item, item.get("label") or "", backend.url):
        return ""
    job = apply_queue.find_by_url(backend.url)
    if not job or job.get("status") != "pending":
        return ""
    apply_queue.mark(job["id"], "skipped", "you didn't approve the Submit")
    left = apply_queue.pending()
    return (f" Karya marked {job['id']} ({job['company']}) as skipped. If the user wants an answer changed they'll say so."
            + (f" Go on with the next picked job: {left[0]['id']} \"{left[0]['title']}\" at {left[0]['company']}." if left else ""))


def _record_submitted(before_url: str, after_url: str, result: str) -> str:
    """A confirmed application goes into the tracker and the pick list without relying on the AI to do it."""
    from .. import apply_queue
    from . import jobs as jobs_mod
    found = re.search(r"RESULT: SUBMITTED - (.+)", result)
    proof = found.group(1).strip()[:150] if found else "confirmed"
    job = apply_queue.find_by_url(before_url, after_url)
    in_queue = job is not None
    if job is None:
        job = apply_queue.find_by_url(before_url, after_url, jobs=list(jobs_mod.cached_jobs().values()))
    if job is None:
        return "\nNEXT: record it with track_application(company, role, url, status=\"applied\")."
    host = urlparse(after_url or before_url).netloc
    saved = jobs_mod.track_application(job.get("company", ""), job.get("title", ""), job.get("url", ""), "applied",
                                       method=f"company site ({host})", notes=f"Submitted by Karya; the page said: {proof}")
    if in_queue:
        apply_queue.mark(job.get("id", ""), "applied", proof)
    text = f"\nKarya saved this in the tracker ({saved}) - no need to call track_application."
    left = apply_queue.pending()
    if left:
        nxt = left[0]
        text += f' Next picked job: {nxt["id"]} "{nxt["title"]}" at {nxt["company"]} ({len(left)} left). Continue with it now.'
    elif apply_queue.load():
        text += " That was the last picked job."
    return text


def _click_precheck(args) -> str | None:
    """Don't press Submit while required fields are still empty (that's how an empty form got 'submitted'), and
    don't click a Yes/No answer the user never gave."""
    backend = _current()
    item = _item(args)
    captcha = captcha_problem(item)
    if captcha:
        return captcha
    if item and _is_choice(item) and item.get("question"):
        problem = answer_problem(item, True)
        if problem:
            return _needs_answers([problem], "NOT CLICKED")
    label = (item or {}).get("label") or ""
    if item is None or not is_submit_click(item, label, backend.url):
        return None
    from .. import outbox
    if is_post_click(item, label, backend.url):
        limit = settings.max_posts_per_day
        if limit and outbox.posts_today() >= limit:
            return (f"NOT CLICKED: today's limit of {limit} posts is reached (more can get the account flagged as spam). "
                    "Continue tomorrow, or the user can raise the limit in Karya's Setup.")
    elif _FORM_HOSTS.search(urlparse(backend.url).netloc.lower()) or "apply" in backend.url.lower():
        from . import jobs as jobs_mod
        limit = settings.max_applications_per_day
        if limit and jobs_mod.applications_today() >= limit:
            return (f"NOT CLICKED: today's limit of {limit} job applications is reached (protects your accounts and "
                    "keeps applications careful). Continue tomorrow, or raise the limit in Karya's Setup.")
    from .. import apply_queue
    done = apply_queue.find_by_url(backend.url)
    if done and done.get("status") == "applied":
        return (f"NOT CLICKED: {done['title']} at {done['company']} is already applied ({done.get('note') or 'done'}). "
                "Don't submit it again; go on with the next picked job.")
    stop = _not_picked(backend.url)
    if stop:
        return stop
    if args.get("confirm_empty"):
        return None
    try:
        empty = (backend.call(backend.form_check, item["id"]) or {}).get("empty") or []
    except Exception:  # noqa: BLE001 - the check is a helper; the approval card still protects the click
        return None
    if not empty:
        return None
    return ("NOT CLICKED (nothing was submitted): these required fields are still empty: "
            + "; ".join(f"\"{e}\"" for e in empty[:12]) + ". Fill them first with the user's real details "
            "(get_application_profile, the resume). Ask the user for anything you don't know (notice period, salary, years "
            "of a specific experience...) and never guess an answer. If a listed field really isn't needed, call "
            "browser_click again with confirm_empty=true (the user will see a warning).")


def _upload_precheck(args) -> str | None:
    """A resume tailored for one company must not be uploaded to another company's application."""
    if args.get("any_resume"):
        return None
    from .resume import tailored_for
    info = tailored_for(args.get("file_path") or "") or {}
    company = info.get("company") or ""
    key = re.sub(r"[^a-z0-9]", "", company.lower())
    if len(key) < 3:
        return None
    backend = _current()
    try:
        context = f"{backend.url} {backend.title} " + backend.call(backend.all_text)[:8000]
    except Exception:  # noqa: BLE001
        return None
    if not context.strip() or key in re.sub(r"[^a-z0-9]", "", context.lower()):
        return None
    job = f" ({info['job_title']})" if info.get("job_title") else ""
    return (f"NOT UPLOADED: this resume was tailored for {company}{job}, but this page doesn't look like {company}'s "
            "application. Run tailor_resume for THIS job and upload that PDF. (Only if the user explicitly wants this "
            "file here, call browser_upload again with any_resume=true.)")


def _type_risk(args):
    item = _item(args)
    label = (item or {}).get("label") or args.get("label") or f"element {args.get('element_id')}"
    text = str(args.get("text", ""))
    if args.get("submit") and not is_search_field(item):
        return CRITICAL, f"Type into \"{label}\" on {_host()} and press Enter (this may submit/send):\n{text[:600]}"
    return SAFE, f"Type into \"{label}\": {text[:200]}"


def _press_risk(args):
    key = str(args.get("key", ""))
    if "enter" in key.lower() and not is_search_field(_current().last_typed):
        return CRITICAL, f"Press {key} on {_host()} (may submit/send)"
    return SAFE, f"Press {key}"


def _check_already_applied(result):
    """If this is a picked job's page and it says the user already applied (LinkedIn "Applied 3 days ago"), mark it
    done so Karya never applies twice, and tell the AI to move on."""
    from .. import apply_queue
    from . import jobs as jobs_mod
    if not isinstance(result, str):
        return result
    backend = _current()
    job = apply_queue.find_by_url(backend.url)
    if not job or job.get("status") != "pending":
        return result
    marked = apply_queue.already_applied(backend.url, result)
    if not marked:
        try:
            marked = apply_queue.already_applied(backend.url, backend.call(backend.all_text))
        except Exception:  # noqa: BLE001 - only a shortcut
            marked = None
    if not marked:
        return result
    jobs_mod.track_application(marked.get("company", ""), marked.get("title", ""), marked.get("url", ""), "applied",
                               method="already applied (seen on the job page)")
    left = apply_queue.pending()
    return (result + f"\nKARYA: {marked['title']} at {marked['company']} is already applied ({marked.get('note', '')}). "
            "Don't apply again." + (f' Next picked job: {left[0]["id"]} "{left[0]["title"]}" at {left[0]["company"]}.'
                                    if left else ""))


@tool("browser_open", "Open a URL in the browser (Karya's own tab in the user's Chrome when Karya Browser Link is "
      "connected, otherwise Karya's Chrome window; logins are kept). Returns a snapshot of the page.", {
    "url": P("string", "URL to open"),
    "new_tab": P("boolean", "Open in a new tab"),
}, required=["url"], group="browser")
def browser_open(url: str, new_tab: bool = False):
    return _check_already_applied(_run("open", url, new_tab))


@tool("browser_snapshot", "List the current page's clickable/typeable elements with [id] numbers (form fields show "
      "their question and *required), plus page text. Use filter to find specific elements on big pages (e.g. 'apply,submit').", {
    "filter": P("string", "Only elements whose label contains one of these comma-separated words"),
    "max_items": P("integer", "Max elements (default 100)"),
}, group="browser")
def browser_snapshot(filter: str | None = None, max_items: int = 100):  # noqa: A002 - tool parameter name
    return _run("snapshot", filter, max_items)


@tool("browser_click", "Click an element by its [id] from the latest snapshot (or by visible text). "
      "Clicks that post/send/submit/apply/pay/delete need the user's approval. After a Submit click the result starts "
      "with RESULT: SUBMITTED / NOT SUBMITTED / UNCONFIRMED.", {
    "element_id": P("integer", "Element id from browser_snapshot"),
    "text": P("string", "Alternatively, the visible text/label of the element"),
    "double": P("boolean", "Double-click"),
    "confirm_empty": P("boolean", "Only if Karya said required fields are empty AND they really aren't needed"),
}, risk=_click_risk, precheck=_click_precheck, group="browser")
def browser_click(element_id: int | None = None, text: str | None = None, double: bool = False, confirm_empty: bool = False):
    before = _current().url
    item = _item({"element_id": element_id, "text": text})
    posting = bool(item) and is_post_click(item, item.get("label") or "", before)
    result = _run("click", element_id, text, double)
    if isinstance(result, str) and "RESULT: SUBMITTED" in result:
        if posting:
            from .. import outbox
            host = urlparse(before).netloc.lower()
            outbox.record_post(host, LAST_COMPOSE.pop(host, ""))
        else:
            result += _record_submitted(before, _current().url, result)
    elif isinstance(result, str) and ("RESULT: UNCONFIRMED" in result or "RESULT: NOT SUBMITTED" in result):
        from .. import apply_queue
        apply_queue.note_result(before, "unconfirmed" if "UNCONFIRMED" in result else "not_submitted")
    return result


@tool("browser_type", "Type text into an input, textarea or rich editor. submit=true presses Enter afterwards. "
      "In a combobox ('Start typing...'), the matching suggestion is picked.", {
    "element_id": P("integer", "Element id from browser_snapshot"),
    "text": P("string", "Text to type (use \\n for new lines)"),
    "clear": P("boolean", "Replace existing text (default true)"),
    "submit": P("boolean", "Press Enter after typing"),
    "label": P("string", "Alternatively, the field's label"),
}, required=["text"], risk=_type_risk, precheck=_type_precheck, group="browser")
def browser_type(text: str, element_id: int | None = None, clear: bool = True, submit: bool = False, label: str | None = None):
    return _run("type_text", element_id, text, clear, submit, label)


@tool("browser_select", "Choose an option in a dropdown.", {
    "element_id": P("integer", "Dropdown element id"),
    "option": P("string", "Visible option text"),
}, required=["element_id", "option"], precheck=_select_precheck, group="browser")
def browser_select(element_id: int, option: str):
    return _run("select", element_id, option)


@tool("browser_check", "Tick or untick a checkbox/radio/switch.", {
    "element_id": P("integer", "Element id"),
    "checked": P("boolean", "true to tick, false to untick"),
}, required=["element_id"], precheck=_check_precheck, group="browser")
def browser_check(element_id: int, checked: bool = True):
    return _run("set_checked", element_id, checked)


@tool("browser_upload", "Upload a file (e.g. the resume PDF tailored for THIS job) using a file input or upload button.", {
    "element_id": P("integer", "File input or upload button id"),
    "file_path": P("string", "Full path of the file to upload"),
    "any_resume": P("boolean", "Only if the user explicitly wants a resume tailored for another job uploaded here"),
}, required=["element_id", "file_path"], risk=lambda a: (CONFIRM, f"Upload {a.get('file_path')} to {_host()}"),
      precheck=_upload_precheck, group="browser")
def browser_upload(element_id: int, file_path: str, any_resume: bool = False):
    return _run("upload", element_id, file_path)


@tool("browser_press", "Press a keyboard key or shortcut (Enter, Escape, Tab, PageDown, Control+Enter...).", {
    "key": P("string", "Key name"),
}, required=["key"], risk=_press_risk, group="browser")
def browser_press(key: str):
    return _run("press", key)


@tool("browser_scroll", "Scroll the page and return a new snapshot.", {
    "direction": P("string", "down, up, top or bottom", enum=["down", "up", "top", "bottom"]),
    "pages": P("number", "How many screens (default 1)"),
}, group="browser")
def browser_scroll(direction: str = "down", pages: float = 1.0):
    return _run("scroll", direction, pages)


@tool("browser_back", "Go back to the previous page.", group="browser")
def browser_back():
    return _run("back")


@tool("browser_read_text", "Read all visible text of the current page (articles, job posts, profiles, confirmations).", {
    "max_chars": P("integer", "Max characters (default 8000)"),
}, group="browser")
def browser_read_text(max_chars: int = 8000):
    return _run("read_text", max_chars)


@tool("browser_screenshot", "Save a screenshot of the current page to the workspace.", {
    "full_page": P("boolean", "Capture the whole page"),
}, group="browser")
def browser_screenshot(full_page: bool = False):
    return _run("screenshot", full_page)


@tool("browser_tabs", "List, switch, open or close Karya's browser tabs.", {
    "action": P("string", "list, switch, new or close", enum=["list", "switch", "new", "close"]),
    "index": P("integer", "Tab index for switch/close"),
    "url": P("string", "URL for a new tab"),
}, group="browser")
def browser_tabs(action: str = "list", index: int | None = None, url: str | None = None):
    return _run("tabs", action, index, url)


@tool("browser_wait", "Wait for the page to finish loading/animating, then snapshot.", {
    "seconds": P("number", "Seconds to wait (max 30)"),
}, group="browser")
def browser_wait(seconds: float = 3):
    return _run("wait", seconds)


def _inside_area(x, y) -> dict | None:
    """The drawn area (canvas, board, map) from the last snapshot that contains this page point."""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    for it in _current().items.values():
        box = it.get("box") if it.get("area") else None
        if box and box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]:
            return it
    return None


def _point_risk(args):
    host = _host()
    if args.get("element_id") is None:
        area = _inside_area(args.get("x"), args.get("y"))
        if area:
            return SAFE, f"Click/drag inside {area.get('tag')} \"{area.get('label', '')}\" on {host}"
        return CRITICAL, (f"Click/drag at page point ({args.get('x')}, {args.get('y')}) on {host}. Karya can't tell what "
                          "is there, so check the Karya tab before approving.")
    item = _item(args)
    if item is None:
        return CONFIRM, f"Click/drag inside element [{args.get('element_id')}] on {host} (not in the last snapshot)"
    if item.get("area"):
        return SAFE, f"Click/drag inside {item.get('tag')} \"{item.get('label', '')}\" on {host}"
    label = item.get("label") or f"element {item.get('id')}"
    return classify_click(item, label, _current().url), f"Click/drag inside \"{label}\" on {host}"


@tool("browser_click_at", "Click at a position: inside an element (element_id, with x and y as 0-1 fractions of its box, "
      "0.5,0.5 = centre) or on the page (no element_id: x, y in CSS pixels; needs approval). For canvas, maps, "
      "drawings and game boards that have no buttons.", {
    "x": P("number", "Horizontal: 0-1 fraction inside element_id, or CSS pixels on the page"),
    "y": P("number", "Vertical: 0-1 fraction inside element_id, or CSS pixels on the page"),
    "element_id": P("integer", "The area to click inside (from browser_snapshot), recommended"),
    "double": P("boolean", "Double-click"),
}, required=["x", "y"], risk=_point_risk, group="browser")
def browser_click_at(x: float, y: float, element_id: int | None = None, double: bool = False):
    return _run("click_at", element_id, x, y, double)


@tool("browser_drag", "Drag with the mouse from one position to another: sliders, maps, drawings, moving things on a "
      "canvas. Same positions as browser_click_at (fractions inside element_id, or page pixels).", {
    "x": P("number", "Start, horizontal"),
    "y": P("number", "Start, vertical"),
    "to_x": P("number", "End, horizontal"),
    "to_y": P("number", "End, vertical"),
    "element_id": P("integer", "The area the positions are inside (recommended)"),
}, required=["x", "y", "to_x", "to_y"], risk=_point_risk, group="browser")
def browser_drag(x: float, y: float, to_x: float, to_y: float, element_id: int | None = None):
    return _run("drag", element_id, x, y, to_x, to_y)


@tool("browser_move_piece", "Make a move on the chess board shown on the page (chess.com, lichess): from_square and "
      "to_square like e2 and e4. Karya clicks/drags the piece and reads the board after, so the result says whether "
      "the move happened. browser_snapshot shows the position.", {
    "from_square": P("string", "Square the piece is on, e.g. e2"),
    "to_square": P("string", "Square to move it to, e.g. e4"),
    "promotion": P("string", "Piece for a pawn promotion: q, r, b or n (default q)"),
    "element_id": P("integer", "The board's id from browser_snapshot (optional)"),
}, required=["from_square", "to_square"], group="browser",
      risk=lambda a: (CONFIRM, f"Chess move {a.get('from_square')}-{a.get('to_square')} on {_host()}"))
def browser_move_piece(from_square: str, to_square: str, promotion: str = "q", element_id: int | None = None):
    return _run("move_piece", element_id, from_square, to_square, promotion)


@tool("browser_close", "Close Karya's browser tabs (or Karya's own window).", group="browser")
def browser_close():
    return _run("close")


COMPOSERS = {
    "x": "https://x.com/intent/post?text={text}",
    "twitter": "https://x.com/intent/post?text={text}",
    "linkedin": "https://www.linkedin.com/feed/?shareActive=true&text={text}",
    "threads": "https://www.threads.net/intent/post?text={text}",
    "bluesky": "https://bsky.app/intent/compose?text={text}",
    "reddit": "https://www.reddit.com/r/{subreddit}/submit?type=TEXT&title={title}&text={text}",
    "whatsapp": "https://web.whatsapp.com/send?phone={phone}&text={text}",
    "facebook": "https://www.facebook.com/",
}


LAST_COMPOSE: dict[str, str] = {}   # host -> text put in its composer (recorded when the Post is confirmed)


def _compose_precheck(args) -> str | None:
    from .. import outbox
    template = COMPOSERS.get(str(args.get("platform", "")).lower())
    if not template:
        return None
    host = urlparse(template).netloc.lower()
    limit = settings.max_posts_per_day
    if limit and outbox.posts_today() >= limit:
        return (f"NOT OPENED: today's limit of {limit} posts is reached (more can get the account flagged as spam). "
                "Continue tomorrow, or the user can raise the limit in Karya's Setup.")
    same = outbox.same_post(host, str(args.get("text", "")))
    if same:
        return (f"NOT OPENED: exactly this text was already posted on {host} on {same.get('time', '')[:16]}. Posting it "
                "again looks like spam. Write a new post, or tell the user it's already up.")
    return None


@tool("social_compose", "Open the post composer of a social site pre-filled with your text (nothing is posted yet). "
      "Then check the snapshot and click the Post/Send button (the user approves).", {
    "platform": P("string", "Where to post", enum=sorted(set(COMPOSERS) | {"instagram"})),
    "text": P("string", "Post / message text"),
    "title": P("string", "Title (Reddit)"),
    "subreddit": P("string", "Subreddit name without r/ (Reddit)"),
    "phone": P("string", "Phone with country code, digits only, e.g. 919876543210 (WhatsApp)"),
}, required=["platform", "text"], group="browser", precheck=_compose_precheck,
      summary=lambda a: f"Open {a.get('platform')} composer with your text (not posted yet)")
def social_compose(platform: str, text: str, title: str = "", subreddit: str = "", phone: str = ""):
    from .skills import guide
    template = COMPOSERS.get(platform.lower())
    if not template:
        book = guide(platform)
        if book:  # e.g. Instagram has no prefilled URL: open the site and follow the steps
            site = {"instagram": "https://www.instagram.com/"}.get(book["platform"], "https://" + book["platform"] + ".com/")
            snap = _run("open", site, False)
            return (snap + "\n\nNEXT - follow these steps in order (keep the user's text ready to type):\n"
                    + "\n".join(f"{n + 1}. {s}" for n, s in enumerate(book["steps"])) + f"\nNote: {book['notes']}")
        return f"ERROR: unknown platform. Use one of {sorted(COMPOSERS)} or instagram."
    url = template.format(text=quote_plus(text), title=quote_plus(title), subreddit=quote_plus(subreddit or "test"),
                          phone=re.sub(r"\D", "", phone))
    LAST_COMPOSE[urlparse(template).netloc.lower()] = text
    snap = _run("open", url, False)
    hint = ("\nNEXT: confirm the text is in the editor (if not, browser_type it into the editor element), "
            "then browser_click the Post/Send button. If a login page is shown, ask the user to log in in that browser tab.")
    return snap + hint


def _secret_risk(args: dict) -> tuple[str, str]:
    from .. import vault
    site = str(args.get("site", ""))
    field = args.get("field") or "password"
    host = urlparse(_current().url).netloc
    if host and vault.site_matches(site, host):
        return SAFE, f"Type your saved {field} for {site} on {host}"
    return CRITICAL, (f"WARNING: type your saved {field} for {site} into a page on '{host or 'unknown'}', which is a "
                      "DIFFERENT site. Only approve if you are sure this is the real login page.")


@tool("browser_fill", "Fill many form fields in one step: {element_id: value}. Works for text inputs, textareas, "
      "comboboxes (the matching suggestion is picked), dropdowns (option text), checkboxes/radios (true/false), "
      "Yes/No buttons (give the id of the button to press, value = its text) and file inputs (file path). Doesn't submit.", {
    "fields": P("object", "Map of element id (from the latest snapshot) to value, e.g. {\"12\": \"Jane\", \"14\": \"India\"}",
                additionalProperties={"type": "string"}),
}, required=["fields"], group="browser",
      summary=lambda a: f"Fill {len(a.get('fields') or {})} field(s) on {_host()}")
def browser_fill(fields: dict):
    if not isinstance(fields, dict) or not fields:
        return "ERROR: fields must be an object like {\"12\": \"value\"}"
    backend = _current()
    allowed, problems, captchas = {}, [], []
    for raw_id, value in fields.items():
        try:
            item = backend.find_item(int(str(raw_id).strip("[] ")))
        except (TypeError, ValueError):
            item = None
        if captcha_problem(item):
            captchas.append(raw_id)
            continue
        problem = answer_problem(item, value)
        if problem:
            problems.append(problem)
        else:
            allowed[raw_id] = value
    held = _needs_answers(problems, "NOT FILLED") + "\n" if problems else ""
    if captchas:
        held += captcha_problem({"label": "captcha"}) + "\n"
    if not allowed:
        return held.strip()
    return held + _run("fill_many", allowed)


@tool("browser_type_secret", "Type a SAVED username or password (from the vault) into a field, without you seeing it. "
      "Use for logins and sign-ups (after list_accounts / request_credentials / vault_new_password).", {
    "element_id": P("integer", "Field id from the latest snapshot"),
    "site": P("string", "Which saved account, e.g. linkedin.com"),
    "field": P("string", "password or username", enum=["password", "username"]),
    "submit": P("boolean", "Press Enter afterwards"),
}, required=["element_id", "site"], risk=_secret_risk, group="accounts")
def browser_type_secret(element_id: int, site: str, field: str = "password", submit: bool = False):
    return _run("type_secret", element_id, site, field or "password", submit)
