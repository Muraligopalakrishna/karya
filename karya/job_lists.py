"""The latest numbered job list the user was shown, so "apply for 4" means #4 in THAT list.

2026-10-19: Maya's 09:00 report ranked 5 jobs (#4 was Neufin). The user wrote "apply for 4"; the chat never saw the
report, read "4" as the job id J4, and the pick-list guard sent it to Cisco from an older list instead.
"""
from __future__ import annotations

import json
import re
import time

from .config import DATA_DIR

FILE = DATA_DIR / "cache" / "shown_jobs.json"
FRESH_HOURS = 24

_URL = re.compile(r"https?://[^\s|)>\]]+")
# A job posting link (not a video or an article): most rows of a job list have one
_JOB_LINK = re.compile(r"job|career|lever\.co|greenhouse|workday|ashby|smartrecruiters|instahyre|cutshort|naukri|"
                       r"linkedin\.com/jobs|wellfound|posting|opening|position|/apply", re.I)
_NUMBERED = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?#?(\d{1,2})(?:\*\*)?[.)]\s+(.+)$")


def _cells(line: str) -> list[str]:
    return [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]


def from_report(text: str) -> list[dict]:
    """Numbered job rows in a report: markdown table rows ("| 4 | Title | Company | City | 80% | link |") or numbered
    lines ("4. Title at Company - link"). Only when most rows link to job postings."""
    rows: dict[int, dict] = {}
    for line in str(text or "").splitlines():
        link = _URL.search(line)
        if not link:
            continue
        url = link.group(0).rstrip(".,;")
        cells = _cells(line) if line.count("|") >= 3 else []
        if cells and re.fullmatch(r"#?\d{1,2}\.?", cells[0]):
            words = [c for c in cells[1:] if c and not _URL.search(c) and not re.fullmatch(r"\d{1,3}\s*%", c)]
            n, title, company = int(re.sub(r"\D", "", cells[0])), (words[0] if words else ""), (words[1] if len(words) > 1 else "")
        else:
            m = _NUMBERED.match(line)
            if not m:
                continue
            n, rest = int(m.group(1)), _URL.sub("", m.group(2))
            rest = re.sub(r"\[([^\]]*)\]\(\s*\)", r"\1", rest)          # [title](link) without the link
            rest = re.sub(r"[\s\-–:|*()\[\]]+$", "", rest).strip()
            parts = re.split(r"\s+(?:at|@)\s+|\s+[-–]\s+", rest)
            title, company = parts[0].strip(" *[]"), (parts[1].strip(" *[]") if len(parts) > 1 else "")
        rows.setdefault(n, {"n": n, "title": title[:120], "company": company[:80], "url": url})
    found = [rows[n] for n in sorted(rows)]
    if len(found) < 1 or sum(bool(_JOB_LINK.search(r["url"])) for r in found) * 2 < len(found):
        return []
    return found


def record(rows: list[dict], source: str) -> None:
    if not rows:
        return
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps({"time": time.time(), "source": str(source)[:80], "jobs": rows}, ensure_ascii=False),
                    encoding="utf-8")


def latest() -> dict | None:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or time.time() - float(data.get("time") or 0) > FRESH_HOURS * 3600:
        return None
    return data if data.get("jobs") else None


# "apply for 4", "apply to 2 and 3", "apply 1,3", "the 4 one", "4th one", "number 4", "#4". Not counts like
# "apply to 4 jobs" or "apply to 10 companies", and not times ("apply by 5 pm").
_ASK = re.compile(r"\b(apply|applying|submit)\b", re.I)
_NOT_A_POSITION = (r"(?!\s*(?:jobs?|roles?|companies|compan|of\b|more|positions?|openings?|times?|days?|hours?|mins?|"
                   r"minutes?|am\b|pm\b|%|lpa\b|k\b))")
_POS = re.compile(r"\b(?:apply|applying|submit)(?:\s+(?:for|to|on|with))?\s+(?:job\s+|number\s+|no\.?\s*|#)?"
                  r"(\d{1,2}(?:\s*(?:,|and|&|\+|/)\s*\d{1,2})*)(?:st|nd|rd|th)?" + _NOT_A_POSITION + r"\b", re.I)
_REF = re.compile(r"(?:\bnumber\s+|\bno\.\s*|#)(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:one|job|role)\b", re.I)


def numbers_in(text: str) -> list[int]:
    text = str(text or "")
    if not _ASK.search(text) or _URL.search(text) or re.search(r"\bJ\d+\b", text, re.I):
        return []                     # a link or a job id says it already
    found: list[int] = []
    groups = [m.group(1) for m in _POS.finditer(text)] + [m.group(1) or m.group(2) for m in _REF.finditer(text)]
    for group in groups:
        for d in re.findall(r"\d{1,2}", group or ""):
            if int(d) not in found:
                found.append(int(d))
    return found


def resolve(text: str) -> list[dict]:
    """The jobs the user means by number, from the latest list they were shown (or [] when unclear)."""
    data = latest()
    nums = numbers_in(text)
    if not data or not nums:
        return []
    by_n = {int(r["n"]): r for r in data["jobs"]}
    if not all(n in by_n for n in nums):
        return []
    return [{**by_n[n], "source": data["source"], "at": time.strftime("%H:%M", time.localtime(data["time"]))}
            for n in nums]


def meaning(refs: list[dict]) -> str:
    """For the task note: which jobs the user means, in plain words, with their links."""
    parts = []
    for r in refs:
        who = f"{r['title']} at {r['company']}" if r.get("company") else r.get("title") or "the job"
        parts.append(f"#{r['n']} in {r['source']} ({r['at']}): {who}, {r['url']}")
    return ("By the number(s) the user means " + "; ".join(parts) + ". Apply to exactly that job (open its link), "
            "not to other jobs from an older pick list.")
