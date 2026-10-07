"""The user's work history with dates: asked once, kept on the master resume, filled into every job form.

The user, 2026-10-07: "it is hallucinating, ask the user the start and end dates and remember them for the rest of
the applications". Karya had typed invented dates (03/2024, 01/2023...) into SmartRecruiters' experience fields.
Now each job's start, end and place come from the user once, through one card, and live on their master resume:
tailored resumes show them (so a form's resume parser fills them in too), autofill types them in the format each
form wants, and any other date in a job's date field is refused."""
from __future__ import annotations

import json
import re

from ..registry import P, tool

MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_PRESENT = re.compile(r"^\W*(present|current(ly)?|now|ongoing|till (date|now)|to date|still (working|there|here|doing it)|"
                      r"i still work|working)\b", re.I)


# ---------------------------------------------------------------- dates as people write them
def parse_when(text) -> dict | None:
    """'Mar 2024', 'march 2024', '03/2024', '2024-03', "Mar '24", 'Present', '2024' ->
    {"month": 3 | None, "year": 2024 | None, "present": bool}; None when there's no date in it."""
    s = str(text or "").strip().lower()
    if not s:
        return None
    if _PRESENT.match(s):
        return {"month": None, "year": None, "present": True}
    m = re.search(r"\b(\d{1,2})\s*[/\-.]\s*((?:19|20)\d{2})\b", s)            # 03/2024, 3-2024
    if m and 1 <= int(m.group(1)) <= 12:
        return {"month": int(m.group(1)), "year": int(m.group(2)), "present": False}
    m = re.search(r"\b((?:19|20)\d{2})\s*[/\-.]\s*(\d{1,2})\b", s)            # 2024-03, 2024/3
    if m and 1 <= int(m.group(2)) <= 12:
        return {"month": int(m.group(2)), "year": int(m.group(1)), "present": False}
    month = re.search(r"\b" + _MON + r"(?![a-z])", s)
    year = re.search(r"\b((?:19|20)\d{2})\b", s)
    if month and not year:
        short = re.search(_MON + r"\s*['’]\s*(\d{2})\b", s)                    # Mar '24
        if short:
            year = re.match(r"(\d{2})", short.group(2))
            yval = 2000 + int(year.group(1)) if year else None
            return {"month": _month_number(month.group(1)), "year": yval, "present": False}
    if month or year:
        return {"month": _month_number(month.group(1)) if month else None,
                "year": int(year.group(1)) if year else None, "present": False}
    return None


def _month_number(word: str) -> int | None:
    word = (word or "").lower()[:3]
    for n, name in enumerate(MONTH_NAMES, 1):
        if name.lower().startswith(word):
            return n
    return None


def month_of(text) -> int | None:
    """A month written on its own: 'March', 'mar', '03', '3'."""
    s = str(text or "").strip().lower()
    if re.fullmatch(r"0?([1-9]|1[0-2])", s):
        return int(s)
    m = re.fullmatch(_MON, s)
    return _month_number(m.group(1)) if m else None


def fmt_when(d: dict | None) -> str:
    if not d:
        return ""
    if d.get("present"):
        return "Present"
    if d.get("month") and d.get("year"):
        return f"{MONTH_NAMES[d['month'] - 1][:3]} {d['year']}"
    return str(d.get("year") or "")


# ---------------------------------------------------------------- the user's jobs and schools
def entries(include_schools: bool = True) -> list[dict]:
    from .resume import load_master
    master = load_master() or {}
    out = []
    for i, x in enumerate(master.get("experience") or []):
        if isinstance(x, dict) and (x.get("company") or x.get("title")):
            out.append({"kind": "job", "index": i, "title": str(x.get("title") or ""),
                        "name": str(x.get("company") or ""), "start": str(x.get("start") or ""),
                        "end": str(x.get("end") or ""), "location": str(x.get("location") or "")})
    if include_schools:
        for i, x in enumerate(master.get("education") or []):
            if isinstance(x, dict) and (x.get("school") or x.get("degree")):
                out.append({"kind": "school", "index": i, "title": str(x.get("degree") or ""),
                            "name": str(x.get("school") or ""), "start": str(x.get("start") or ""),
                            "end": str(x.get("end") or ""), "location": ""})
    return out


def _label(e: dict) -> str:
    return f"{e['title']} at {e['name']}" if e["title"] and e["name"] else (e["name"] or e["title"])


def questions(include_schools: bool = False) -> list[dict]:
    """Card questions for each job (and, if asked, school) whose start, end or place isn't known yet."""
    from .. import answers
    city = answers.saved_answer("Which city do you live in?") or ""
    out = []
    for e in entries(include_schools):
        name = _label(e)
        start, end = parse_when(e["start"]), parse_when(e["end"])
        if not start or not start.get("month") or not start.get("year"):
            out.append({"q": f"{name}: when did you start? (month and year)", "options": [],
                        "hint": "e.g. Mar 2024", "value": e["start"], "_index": e["index"], "_kind": e["kind"],
                        "_field": "start"})
        if not end or not (end.get("present") or (end.get("month") and end.get("year"))):
            out.append({"q": f"{name}: when did it end?", "options": ["Present (still doing it)"],
                        "hint": "e.g. Aug 2025, or Present", "value": e["end"], "_index": e["index"],
                        "_kind": e["kind"], "_field": "end"})
        if e["kind"] == "job" and not e["location"]:
            out.append({"q": f"{name}: where was it? (city, or Remote)",
                        "options": [o for o in (city.title() if city else "", "Remote") if o], "hint": "e.g. Hyderabad",
                        "value": "", "_index": e["index"], "_kind": e["kind"], "_field": "location"})
    return out


def save_answers(asked: list[dict], given: dict) -> list[str]:
    """Write the user's answers onto their master resume. Unreadable dates are left for the next card."""
    from .resume import MASTER_FILE, load_master
    master = load_master()
    if not master:
        return []
    saved = []
    for q in asked:
        raw = str((given or {}).get(q["q"], "")).strip()
        if not raw:
            continue
        section = master.get("experience" if q["_kind"] == "job" else "education") or []
        if not 0 <= q["_index"] < len(section) or not isinstance(section[q["_index"]], dict):
            continue
        entry = section[q["_index"]]
        if q["_field"] == "location":
            entry["location"] = raw[:80]
        else:
            d = parse_when(raw)
            if not d or (q["_field"] == "start" and d.get("present")) or not (d.get("present") or d.get("year")):
                continue
            entry[q["_field"]] = fmt_when(d)
        saved.append(f"{q['q'].split(':')[0]}: {q['_field']} = {entry[q['_field']]}")
    if saved:
        MASTER_FILE.write_text(json.dumps(master, ensure_ascii=False, indent=1), encoding="utf-8")
    return saved


@tool("ask_job_dates", "Ask the user, in ONE card, when each job on their resume started and ended and where it was "
      "(and their school dates if a form needs them). Saved on their resume and used for every job form, so they're "
      "asked only once. Use this, never ask_user, for job dates; never type a job date the user didn't give.", {
    "include_education": P("boolean", "Also ask for school/college start and end months"),
}, group="jobs")
def ask_job_dates(include_education: bool = False):
    return "ERROR: ask_job_dates only works from the chat (the user answers in a card)."


@tool("work_history", "The user's jobs and schools from their resume, with the start/end dates and places Karya knows, "
      "and which are still missing (then call ask_job_dates).", group="jobs")
def work_history():
    rows = [{k: v for k, v in {"what": e["kind"], "title": e["title"], "at": e["name"], "start": e["start"],
                               "end": e["end"], "where": e["location"]}.items() if v} for e in entries()]
    missing = [q["q"] for q in questions()]
    return {"history": rows, "missing": missing,
            "next": "Call ask_job_dates for the missing ones." if missing else "Everything is known."}


# ---------------------------------------------------------------- a form's employment and education blocks
_COMPANY = re.compile(r"^(current |present |most recent |latest |previous )?(company|employer|organi[sz]ation|firm)"
                      r"( name)?$", re.I)
_SCHOOL = re.compile(r"^(school|university|college|institut(e|ion)|school or university)( name)?$", re.I)
_TITLE = re.compile(r"^(current |present |most recent |latest )?(job |your )?(title|position|designation)( held)?$", re.I)
_DEGREE = re.compile(r"^(degree|qualification|course|discipline|field of study|major)( name)?$", re.I)
_PLACE = re.compile(r"^(office |work |job )?(location|city)$", re.I)
_CURRENT = re.compile(r"current(ly)?\s+(role|job|position|employer|company|work|study)|i\s+(currently|still)\s+"
                      r"(work|study)|\bwork here\b|\bstudy here\b|^\W*(present|current|ongoing)\W*$|still (working|there)",
                      re.I)
_START_WORDS = r"start(ed|ing)?|from|joined|joining|begin|began|commenced"
_END_WORDS = r"end(ed|ing)?|to|until|till|left|leaving|finish(ed)?|graduat(ion|ed)|completed|completion"
_DATE_LABEL = re.compile(r"^(?:(?:date|month|year)\s+(?:of\s+)?)?(?P<word>" + _START_WORDS + "|" + _END_WORDS +
                         r")(?:\s+(?:date|month|year|on))*(?:\s*\(\s*(?:mm|yyyy|month|year)[^)]*\))?$", re.I)
_DATE_HINT = re.compile(r"pick a date|mm\s*/\s*(yy|yyyy)|month\s*/\s*year|yyyy\s*-\s*mm|dd\s*/\s*mm|mm\s*/\s*dd|date",
                        re.I)


def _plain(text) -> str:
    text = re.sub(r"[✱*]|\(required\)|\brequired\b|\(optional\)", " ", str(text or ""), flags=re.I)
    return re.sub(r"\s+", " ", re.sub(r"[^\w/&()'-]+", " ", text)).strip().lower()


def date_role(item: dict) -> tuple[str, str] | None:
    """("start" | "end", "month" | "year" | "full") for a job or school date field, else None."""
    if not item or item.get("tag") not in ("input", "select", "textarea", None) and item.get("role") != "combobox":
        return None
    label, question = _plain(item.get("label")), _plain(item.get("question"))
    placeholder = _plain(item.get("placeholder"))
    kind = (item.get("type") or "").lower()
    if kind in ("checkbox", "radio", "file", "hidden", "submit", "button", "email", "tel", "password", "url"):
        return None
    side = None
    for text in (label, question):
        m = _DATE_LABEL.match(text)
        if m:
            side = "start" if re.fullmatch(_START_WORDS, m.group("word"), re.I) else "end"
            break
    if side is None and label in ("month", "year", "mm", "yyyy") and question:
        m = _DATE_LABEL.match(question)          # Workday: label "Month" inside the question "From"
        if m:
            side = "start" if re.fullmatch(_START_WORDS, m.group("word"), re.I) else "end"
    if side is None:
        return None
    looks_date = kind in ("date", "month") or bool(_DATE_HINT.search(placeholder)) or bool(
        re.search(r"\b(date|month|year|mm|yyyy)\b", f"{label} {question}"))
    if not looks_date:
        return None
    words = f"{label} {placeholder}"
    if re.search(r"\bmonth\b|^mm$", words) and not re.search(r"\byear\b|yyyy", words):
        part = "month"
    elif re.search(r"\byear\b|^yyyy$", words) and not re.search(r"\bmonth\b|\bmm\b", words):
        part = "year"
    else:
        part = "full"
    return side, part


def field_role(item: dict) -> str | None:
    """company | school | title | degree | location | current | start-month... for one field of a form."""
    if not item:
        return None
    label = _plain(item.get("label") or item.get("question"))
    kind = (item.get("type") or "").lower()
    if kind in ("checkbox", "switch") or item.get("role") in ("checkbox", "switch"):
        text = f"{_plain(item.get('label'))} {_plain(item.get('question'))}"
        return "current" if _CURRENT.search(text) else None
    if kind in ("radio", "file", "hidden", "submit", "button", "email", "tel", "password"):
        return None
    if item.get("tag") not in ("input", "select", "textarea") and item.get("role") != "combobox":
        return None
    dated = date_role(item)
    if dated:
        return f"{dated[0]}-{dated[1]}"
    for role, rx in (("company", _COMPANY), ("school", _SCHOOL), ("title", _TITLE), ("degree", _DEGREE),
                     ("location", _PLACE)):
        if rx.match(label):
            return role
    return None


_NOW = re.compile(r"^(current|present|most recent|latest)\b")


def _pos(it: dict) -> tuple:
    return (it.get("y", 0), it.get("x", 0))


def _unlabeled_box(it: dict) -> bool:
    return ((it.get("type") or "").lower() == "checkbox" or it.get("role") in ("checkbox", "switch")) and \
        not _plain(it.get("label")) and not _plain(it.get("question"))


def blocks(items: list[dict]) -> list[dict]:
    """A form's employment/education blocks, top to bottom: {"entity": "company"|"school"|None, "fields": [(role, item)]}.
    A block starts at its company/school field; fields below it (until the next block) belong to it. A title on the
    same row just before the company (SmartRecruiters) belongs to the company's block."""
    rows = [it for it in items if not it.get("area")]
    if any("y" in it for it in rows):
        rows = sorted(rows, key=_pos)
    out: list[dict] = []
    cur = None
    loose: list = []
    for it in rows:
        role = field_role(it)
        if not role and cur is not None and _unlabeled_box(it) and any(r.startswith("end-") for r, _ in cur["fields"]) \
                and not any(r == "current" for r, _ in cur["fields"]):
            role = "current"        # SmartRecruiters: the "I currently work here" box after "To" has no label
        if not role:
            continue
        now = bool(_NOW.match(_plain(it.get("label") or it.get("question"))))
        if role in ("company", "school"):
            pulled = [f for f in loose if abs(f[1].get("y", -999) - it.get("y", 999)) <= 40] if not now else []
            loose = []
            cur = {"entity": role, "anchor": it, "fields": pulled, "current": now}
            out.append(cur)
            continue
        if role in ("title", "degree", "location"):
            # "Current designation" is about the latest job, not the block above; and once a block has its dates,
            # the fields after them are other questions (Razorpay asks "Current Designation" below its employment),
            # unless the next company on the same row claims them (SmartRecruiters: Title, then Company)
            if now:
                continue
            if cur is None or any(r.startswith("end-") or r == "current" for r, _ in cur["fields"]):
                if role != "location":
                    loose.append((role, it))
                continue
        if cur is None:
            cur = {"entity": None, "anchor": None, "fields": []}
            out.append(cur)
        cur["fields"].append((role, it))
    return [b for b in out if any(r.split("-")[0] in ("start", "end", "current") for r, _ in b["fields"]) or b["entity"]]


def match_entry(block: dict, people: list[dict], position: int = 0) -> dict | None:
    """The resume job (or school) a block is about: by the company/school written in it, else by its title; an empty
    block is the next one in resume order (a form's first employment block is the latest job)."""
    from .job_sources import similar
    kind = "school" if block.get("entity") == "school" else "job"
    pool = [e for e in people if e["kind"] == kind]
    anchor = block.get("anchor") or {}
    name = str(anchor.get("value") or "").strip()
    title = next((str(it.get("value") or "").strip() for role, it in block["fields"] if role in ("title", "degree")), "")
    if name:
        hits = [e for e in pool if e["name"] and similar(e["name"], name)]
        if len(hits) > 1 and title:
            hits = [e for e in hits if similar(e["title"], title)] or hits
        return hits[0] if hits else None
    if title:
        hits = [e for e in pool if e["title"] and similar(e["title"], title)]
        if len(hits) == 1:
            return hits[0]
    return pool[position] if 0 <= position < len(pool) and not title else None


def known_date(entry: dict | None, side: str) -> dict | None:
    return parse_when((entry or {}).get(side))


def value_for(item: dict, part: str, d: dict | None) -> str | None:
    """The date to type into this field, in the format it asks for (None when it can't be filled from d)."""
    if not d or d.get("present") or not d.get("year"):
        return None
    month, year = d.get("month"), d["year"]
    if part == "year":
        return str(year)
    if not month:
        return None
    if part == "month":
        return MONTH_NAMES[month - 1]
    kind = (item.get("type") or "").lower()
    hint = _plain(item.get("placeholder"))
    if kind == "month" or "yyyy-mm" in hint.replace(" ", ""):
        return f"{year}-{month:02d}"
    if kind == "date":
        return f"{year}-{month:02d}-01"
    compact = hint.replace(" ", "")
    if "dd/mm/yyyy" in compact:
        return f"01/{month:02d}/{year}"
    if "mm/dd/yyyy" in compact:
        return f"{month:02d}/01/{year}"
    if re.search(r"mm/yy(?!yy)", compact):
        return f"{month:02d}/{year % 100:02d}"
    return f"{month:02d}/{year}"


# ---------------------------------------------------------------- the guard: only the user's own dates
def _same(value, d: dict, part: str) -> bool:
    if part == "month":
        return bool(d.get("month")) and month_of(value) == d["month"]
    got = parse_when(value)
    if not got:
        return False
    if d.get("present"):
        return bool(got.get("present"))
    if part == "year":
        return got.get("year") == d.get("year")
    if got.get("year") != d.get("year"):
        return False
    return not d.get("month") or not got.get("month") or got["month"] == d["month"]


def block_of(item: dict, items: list[dict]) -> tuple[dict | None, int]:
    """The block a field is in, and how many blocks of the same kind come before it ("current company": 0)."""
    all_blocks = blocks(items)
    for n, b in enumerate(all_blocks):
        if any(it.get("id") == item.get("id") for _, it in b["fields"]) or (b.get("anchor") or {}).get("id") == item.get("id"):
            return b, 0 if b.get("current") else sum(1 for other in all_blocks[:n]
                                                     if other.get("entity") == b.get("entity") and not other.get("current"))
    return None, 0


def is_history_block(block: dict | None) -> bool:
    """An employment/education block: it names a company or school, or asks both a start and an end."""
    if not block:
        return False
    roles = {r.split("-")[0] for r, _ in block["fields"]}
    return bool(block.get("entity")) or {"start", "end"} <= roles


def check(item: dict, value, items: list[dict]) -> tuple[bool, str | None]:
    """(is this a job/school date field, why the value may not go in). Unknown dates send the AI to ask_job_dates.
    A lone "Start date" / "Date of joining" outside any employment block is "when can you start", not checked here."""
    role = date_role(item)
    block, position = block_of(item, items)
    current = field_role(item) == "current" or (block is not None and any(
        r == "current" and it.get("id") == item.get("id") for r, it in block["fields"]))
    if (not role and not current) or not is_history_block(block):
        return False, None
    text = str(value if value is not None else "").strip()
    if not text or (current and text.lower() in ("false", "0", "off", "no", "unchecked")):
        return True, None
    people = entries()
    entry = match_entry(block, people, position)
    label = str(item.get("label") or item.get("question") or "date")[:60]
    if current:
        end = known_date(entry, "end")
        if entry is None or end is None:
            return True, (f'"{label or "currently work here"}": Karya doesn\'t know if the user still works there. Call '
                          "ask_job_dates (one card for all jobs, saved for every form), then fill it")
        return True, None if end.get("present") else f'"{label}": {_label(entry)} ended {entry["end"]}, so it isn\'t current'
    side, part = role
    if entry is None:
        known = [d for e in people for d in (known_date(e, side),) if d]
        if any(_same(text, d, part) for d in known):
            return True, None
        return True, (f'"{label}": "{text[:30]}" isn\'t a date the user gave Karya. Fill this block\'s company first (or '
                      "call ask_job_dates); never type a date the user didn't give")
    d = known_date(entry, side)
    if d is None or (part != "year" and not d.get("present") and not d.get("month")):
        verb = "started" if side == "start" else "finished"
        return True, (f'"{label}": Karya doesn\'t know when the user {verb} as {_label(entry)}. Call ask_job_dates (one '
                      "card for all jobs; saved on their resume for every form), then fill it in. Never guess a date")
    if _same(text, d, part):
        return True, None
    return True, (f'"{label}": the user said {_label(entry)} {"started" if side == "start" else "ended"} '
                  f'{entry[side]}, not "{text[:30]}"')
