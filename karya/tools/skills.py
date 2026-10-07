"""Ordered playbooks for tasks people get wrong in the browser - posting a reel, a LinkedIn update, an X post.

Karya reads the right playbook before it starts, follows the steps in order, and verifies the result. The Instagram one
exists because Karya once muted a reel and cropped it: the fix is to keep the audio on and pick "Original" size."""
from __future__ import annotations

from ..registry import P, tool

# Karya's browser keeps the user's logins (cookies), so the vault is only a fallback. An agent once told the user
# "you're not logged in to X" just because x.com wasn't in list_accounts, while the browser was signed in.
SIGNED_IN_FIRST = ("Karya's browser keeps the user's logins, so open the site FIRST and look: a site missing from "
                   "list_accounts does NOT mean they're logged out.")


def _login_step(site: str) -> str:
    return (f"Only if the page shows a sign-in screen: log in with browser_type_secret for {site} (list_accounts shows "
            "the saved ones). No saved login: ask the user to sign in in Karya's browser window, then continue. "
            "CAPTCHA/OTP: ask the user.")


PLAYBOOKS: dict[str, dict] = {
    "instagram": {
        "title": "Post a reel/video or photo on Instagram (web, instagram.com)",
        "needs_login": True,
        "steps": [
            "browser_open https://www.instagram.com/. " + SIGNED_IN_FIRST,
            _login_step("instagram.com"),
            "Click the Create button (the + / 'New post'), then 'Post' in its menu (that only opens the upload "
            "window; Instagram's final button is 'Share').",
            "browser_upload the file (full path) on 'Select from computer'. Wait until the crop screen with 'Next' shows; "
            "if 'Select from computer' is still there, the file didn't take: browser_upload once more, then tell the user.",
            "CROP SCREEN - this is where size is set. Click the crop/aspect icon (two diagonal arrows, bottom-left) and "
            "choose 'Original' so the video/photo keeps its real size and isn't cut to a square. Do NOT leave it on 1:1.",
            "For a video, DON'T mute it: leave the sound/audio on (don't toggle the speaker off and don't replace the "
            "audio with a music track unless the user asked). Then click Next.",
            "Skip filters/trim (Next again) unless the user asked for them.",
            "Type the caption the user gave (browser_type into the caption box). Add hashtags only if they asked.",
            "Click Share. Wait, then verify: the page shows 'Your post has been shared' (or the reel appears on the "
            "profile) and the video plays with sound. Only then tell the user it's posted.",
        ],
        "notes": "Instagram has no prefilled share URL, so Karya does it step by step on the site. Keep original size "
                 "and keep the audio - those are the two things to get right.",
    },
    "linkedin": {
        "title": "Post an update on LinkedIn (web)",
        "needs_login": True,
        "steps": [
            "WITH A VIDEO/PHOTO, media first, then the text: browser_open https://www.linkedin.com/feed/ (the post box "
            "has 'Video' and 'Photo'). " + SIGNED_IN_FIRST,
            _login_step("linkedin.com"),
            "browser_upload the file on 'Video' (or 'Photo'); if an Editor opens with 'Upload from computer', "
            "browser_upload on that. Karya chooses the file in the file picker, so never just click those buttons. If "
            "the Editor's button says 'Loading', the page is waiting for a file: call browser_upload with it anyway.",
            "Wait until the video/photo shows in the Editor, then click Next: the post editor opens with it attached.",
            "Now type the user's text into the post editor (browser_type). Snapshot: the text AND the media must both "
            "be there.",
            "TEXT ONLY (no file): social_compose(platform='linkedin', text=...) opens the composer with your text, or "
            "click 'Start a post' and type it.",
            "Set the audience (Anyone/Connections) only if the user asked; the default is fine.",
            "Click Post. Verify the page shows 'Post successful' / the update appears (with the video, if there was "
            "one), then tell the user.",
        ],
        "notes": "Posting on LinkedIn is free. Never post without the file the user gave: if it can't be attached, "
                 "stop and tell them.",
    },
    "x": {
        "title": "Post on X / Twitter (web)",
        "needs_login": True,
        "steps": [
            "WITH A VIDEO/PHOTO, media first, then the text: browser_open https://x.com/home and browser_upload the "
            "file on the composer's media button / file input. " + SIGNED_IN_FIRST,
            _login_step("x.com"),
            "Wait for the upload to finish (the preview shows and Post is enabled), then type the user's text into the "
            "composer (browser_type).",
            "TEXT ONLY (no file): social_compose(platform='x', text=...) opens the composer with your text.",
            "Click Post. Verify it shows as sent / appears on the timeline, then tell the user.",
        ],
        "notes": "Posting on X is FREE. X Premium (paid) only adds longer posts, edit and a badge; it is NOT needed to "
                 "post. A brand-new or limited account may be rate-limited by X itself - that's not a Karya limit. "
                 "Never post without the file the user gave.",
    },
    "reddit": {
        "title": "Post on Reddit (web)",
        "needs_login": True,
        "steps": [
            "social_compose(platform='reddit', text=..., title=..., subreddit=...) opens the submit page.",
            "Check the subreddit's rules/flair; set a flair if it's required. Click Post and verify it appears.",
        ],
        "notes": "Posting on Reddit is free. Many subreddits need a flair or a minimum karma.",
    },
}
ALIASES = {"insta": "instagram", "ig": "instagram", "reel": "instagram", "reels": "instagram", "twitter": "x",
           "tweet": "x", "linked in": "linkedin"}


def for_task(text: str) -> str:
    """The platform a 'post this' request is about, from the user's words (or '')."""
    low = (text or "").lower()
    for name in PLAYBOOKS:
        if name in low:
            return name
    for alias, name in ALIASES.items():
        if alias in low:
            return name
    return ""


def guide(platform: str) -> dict | None:
    key = (platform or "").strip().lower()
    key = ALIASES.get(key, key)
    book = PLAYBOOKS.get(key)
    if not book:
        return None
    return {"platform": key, "title": book["title"], "login_needed": book["needs_login"],
            "steps": book["steps"], "notes": book["notes"]}


@tool("how_to_post", "The exact steps and gotchas for posting on a site (instagram, linkedin, x, reddit). Read it "
      "BEFORE you start posting so you do it in the right order (e.g. Instagram: keep original size and audio).", {
    "platform": P("string", "instagram, linkedin, x or reddit"),
}, required=["platform"], group="browser")
def how_to_post(platform: str):
    book = guide(platform)
    if not book:
        return f"ERROR: no playbook for '{platform}'. I have: {', '.join(PLAYBOOKS)}."
    return book
