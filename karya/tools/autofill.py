"""One-step autofill for job application forms: everything Karya already knows goes in at once.

The fast job apps fill a whole form from the user's saved profile in a second and leave the rest to them. Karya used
to spend an AI step on almost every field and lost many more to retries (2026-10-06: 51 minutes and 267 steps without
one submitted application). Now one call fills names, contact details, links, location, the resume made for this job
and every question the user answered before, and returns only what is still open. It runs by itself when Karya opens a
job the user picked. It never guesses (every value goes through the same answer guard as typing) and never submits."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .. import answers
from ..registry import P, tool
from . import browser as B

# Profile field -> pattern on the field's label, name or placeholder. Checked in order.
TEXT_FIELDS = (
    ("email", r"e-?mail"),
    ("first_name", r"\bfirst[\s_-]*name\b|\bgiven[\s_-]*names?\b|\bfname\b|^\W*first\W*$|\bpreferred (first )?name\b"),
    ("last_name", r"\blast[\s_-]*name\b|\bsurname\b|\bfamily[\s_-]*name\b|\blname\b|^\W*last\W*$"),
    ("full_name", r"^\W*(your |full |legal |candidate'?s? )?name\W*$|\bfull[\s_-]*name\b|\blegal name\b"),
    ("phone", r"\bphone\b|\bmobile\b|\bcell\b|contact (number|no)|telephone|whatsapp"),
    ("linkedin", r"linked\s*in"),
    ("github", r"git\s*hub"),
    ("portfolio", r"portfolio|personal (web)?site|^\W*website\b|other website|\bblog\b"),
    ("location", r"^\W*(current |your )?(location|city)\b|\bcity of residence\b|current (city|location)|"
                 r"where are you (based|located)|what is your (current )?location"),
    ("country", r"^\W*(your |current )?country( of residence)?\W*$|what is your (current )?country|"
                r"country (you live in|of residence)"),
)
_COMPILED = [(field, re.compile(pattern, re.I)) for field, pattern in TEXT_FIELDS]
# someone else's details, or a part of the field that isn't the value itself
_NOT_MINE = re.compile(r"refer|reference|emergency|manager|supervisor|recruiter|company|employer|school|universit|"
                       r"college|spouse|parent|father|mother|guardian|friend|relative", re.I)
_NOT_THE_VALUE = {"phone": re.compile(r"code|extension|\bext\b|device|type", re.I),
                  "location": re.compile(r"prefer|relocat|willing|work location|job location|office|desired", re.I),
                  "email": re.compile(r"subscribe|newsletter|alerts?\b|marketing", re.I)}
_URL = re.compile(r"^(https?://|www\.)\S+$|^[\w.-]+\.[a-z]{2,}(/\S*)?$", re.I)
_RESUME = re.compile(r"resume|r[ée]sum[ée]|\bcv\b|curriculum", re.I)
_PLACEHOLDER = re.compile(r"^\W*(select|choose|please (select|choose)|pick|--|none selected)\b|^\W*$", re.I)
_TEXT_TYPES = {"", "text", "email", "tel", "url", "number"}
_SKIP_TYPES = {"hidden", "submit", "button", "reset", "image", "password", "search"}
# Rules rather than facts (e.g. "no sponsorship in India, yes elsewhere"): the AI applies them to the job's country.
_RULE_TOPICS = {"work_authorization", "relocation"}


_CODE_FIELD = re.compile(r"(country|dial(l?ing)?)\s*(phone\s*)?code|phone\s*code|\bcountry\s*prefix", re.I)
_PHONE_ONLY = re.compile(r"\bphone\b|\bmobile\b", re.I)


def _text(it: dict) -> str:
    return " ".join(str(it.get(k) or "") for k in ("label", "name", "placeholder", "key"))


_OTHER_UPLOAD = re.compile(r"cover|letter|photo|picture|headshot|avatar|transcript|certificate|portfolio|sample|"
                           r"writing|id proof|passport|other", re.I)


def _resume_input(items: list[dict], page_text: str = "") -> dict | None:
    """The file input for the resume: named so (label, name, id like Greenhouse's id="resume"), or else the first
    upload that isn't for something else (cover letter, photo...) on a form that asks for a resume."""
    files = [it for it in items if it.get("type") == "file" and not it.get("disabled")]
    named = [it for it in files if _RESUME.search(_text(it) + " " + str(it.get("question") or ""))]
    if named:
        return named[0]
    asks = _RESUME.search(page_text) or any(_RESUME.search(_text(it) + " " + str(it.get("question") or ""))
                                             for it in items)
    plain = [it for it in files if not _OTHER_UPLOAD.search(_text(it) + " " + str(it.get("question") or ""))]
    return plain[0] if asks and plain else None


def _choice(it: dict) -> bool:
    return it.get("type") in ("radio", "checkbox") or it.get("role") in ("radio", "checkbox", "switch") or (
        it.get("tag") == "button" and bool(it.get("question")))


def _field(it: dict) -> bool:
    if it.get("disabled") or it.get("area") or B.is_search_field(it):
        return False
    if it.get("tag") in ("input", "textarea", "select"):
        return (it.get("type") or "") not in _SKIP_TYPES
    return bool(it.get("editable") or it.get("role") in ("combobox", "textbox") or _choice(it))


def _empty(it: dict) -> bool:
    if "checked" in it and _choice(it):
        return not it["checked"]
    value = str(it.get("value") or "").strip()
    return not value or (it.get("tag") == "select" and bool(_PLACEHOLDER.match(value)))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def pick_option(options: list[str], answer: str) -> str | None:
    """The one option that says the same as the user's answer ("Yes" for "yes, I'm 24"), or None."""
    a = _norm(answer)
    real = [(o, _norm(o)) for o in options if _norm(o) and not _PLACEHOLDER.match(o) and len(o) < 40]  # 40 = cut off
    if not a or not real:
        return None
    for o, n in real:
        if n == a:
            return o
    yes_no = "yes" if re.match(r"(yes|y|true)\b", a) else "no" if re.match(r"(no|n|false)\b", a) else ""
    if yes_no:
        hits = [o for o, n in real if n == yes_no or n.startswith(yes_no + " ")]
        if len(hits) == 1:
            return hits[0]
    for test in (lambda n: a.startswith(n) or n.startswith(a), lambda n: len(n) >= 3 and (n in a or a in n)):
        hits = [o for o, n in real if test(n)]
        if len(hits) == 1:
            return hits[0]
    return None


def _profile_value(it: dict, profile: dict) -> str | None:
    text = _text(it)
    if _NOT_MINE.search(text):
        return None
    for field, rx in _COMPILED:
        if not rx.search(text):
            continue
        if field in _NOT_THE_VALUE and _NOT_THE_VALUE[field].search(text):
            return None
        if field == "country":
            value = country_of(profile) or ""
        else:
            value = str(profile.get(field) or "").strip()
        if field in ("linkedin", "github", "portfolio") and not _URL.match(value):
            return None
        return value or None
    return None


def country_of(profile: dict) -> str | None:
    """The user's country: their profile's, the end of "City, Country", or their resume's location ("India")."""
    for place in (profile.get("country"), profile.get("location")):
        if place and ("," in str(place) or str(place).strip().lower() in _COUNTRIES):
            return str(place).split(",")[-1].strip()
    from .resume import load_master
    place = str((load_master() or {}).get("location") or "")
    return place.split(",")[-1].strip() or None


_COUNTRIES = {"india", "united states", "usa", "united kingdom", "uk", "canada", "germany", "singapore", "australia",
              "united arab emirates", "uae", "netherlands", "ireland", "france", "japan"}


def _known_answer(question: str) -> str | None:
    """The user's own answer to this question, if they gave one before."""
    category = answers.classify(question)
    if category == "history_dates":
        return None  # which job's date it is isn't clear from one field: the AI fills these from the user's answers
    if category in _RULE_TOPICS:
        return answers.saved_answers().get(answers._norm(question))  # only an answer to this exact question
    return answers.saved_answer(question)


def _job_here(url: str) -> dict | None:
    from .. import apply_queue
    from . import jobs
    return apply_queue.job_for_page(url) or apply_queue.find_by_url(url, jobs=list(jobs.cached_jobs().values()))


def resume_for(company: str) -> str | None:
    """The newest resume PDF Karya tailored for this company (None if there isn't one yet)."""
    from . import job_sources as S
    from . import resume
    try:
        index = json.loads(resume._resume_index_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    best = None
    for path, info in index.items():
        if company and S.similar(info.get("company") or "", company) and Path(path).exists():
            if best is None or str(info.get("made") or "") > best[1]:
                best = (path, str(info.get("made") or ""))
    if not best:
        return None
    return os.path.realpath(best[0])   # the index keys are lower-case: send the file under its real name


def _label(it: dict) -> str:
    return answers.short(it.get("label") or it.get("question") or it.get("name") or f"[{it.get('id')}]")


# "Company name" / "Current company" and "Title" / "Current designation" text boxes: the latest job on the resume.
_COMPANY_FIELD = re.compile(r"^\W*(current |present |most recent |latest )?(company|employer|organi[sz]ation)( name)?\W*$",
                            re.I)
_TITLE_FIELD = re.compile(r"^\W*(current |present |most recent |latest )?(job )?(title|position|designation)\W*$", re.I)


def latest_job() -> dict:
    """The newest job on the user's master resume ({} if there's none)."""
    from . import resume
    master = resume.load_master() or {}
    return next((e for e in master.get("experience") or [] if e.get("company") or e.get("title")), {})


def _job_value(it: dict, job: dict, used: set) -> str | None:
    if it.get("tag") != "input" or it.get("role") == "combobox" or B.wants_pick(it) or not job:
        return None
    label = str(it.get("label") or it.get("question") or "")
    current = bool(re.match(r"^\W*(current|present|most recent|latest)\b", label, re.I))
    for kind, rx in (("company", _COMPANY_FIELD), ("title", _TITLE_FIELD)):
        if (current or kind not in used) and rx.match(label) and str(job.get(kind) or "").strip():
            if not current:
                used.add(kind)            # only the first plain box: further ones are older jobs
            return str(job[kind]).strip()
    return None


def _value_for(it: dict, profile: dict) -> str | None:
    """The user's own answer to exactly this question first (one user gave only their family name for "Last name"),
    then the profile, then their answer to a similar question."""
    for q in (it.get("label"), it.get("question")):
        if q and not answers.classify(q):
            exact = answers.saved_answers().get(answers._norm(q))
            if exact:
                return str(exact)
    value = _profile_value(it, profile)
    if value is None:
        value = next((v for v in (_known_answer(q) for q in (it.get("label"), it.get("question")) if q) if v), None)
    return value


def history_plan(items: list[dict]) -> tuple[dict, dict, set]:
    """The employment/education blocks of a form, filled from the user's work history (their own dates only):
    ({id: value}, {id: name}, ids of every field inside such a block)."""
    from . import work_history as W
    people = W.entries()
    fills, names, inside = {}, {}, set()
    seen: dict = {}
    for block in W.blocks(items):
        if not W.is_history_block(block):
            continue
        if block.get("current"):
            position = 0                     # "Current company": the latest job
        else:
            position = seen.get(block.get("entity"), 0)
            seen[block.get("entity")] = position + 1
        fields = ([("anchor", block["anchor"])] if block.get("anchor") else []) + block["fields"]
        inside.update(it["id"] for _, it in fields)
        entry = W.match_entry(block, people, position)
        if entry is None:
            continue
        end = W.known_date(entry, "end")
        for role, it in fields:
            if it.get("disabled") or (not _empty(it) and role != "current"):
                continue
            value = None
            if role == "anchor":
                value = entry["name"]
            elif role in ("title", "degree"):
                value = entry["title"]
            elif role == "location":
                value = entry.get("location") or None
            elif role == "current":
                value = "true" if end and end.get("present") and _empty(it) else None
            elif role.startswith(("start-", "end-")):
                side, part = role.split("-", 1)
                if side == "end" and end and end.get("present"):
                    continue                 # ticked "currently work here" instead
                value = W.value_for(it, part, W.known_date(entry, side))
                if value and it.get("tag") == "select":
                    value = pick_option(it.get("options") or [], value)
            if value and not B.answer_problem(it, value):
                fills[str(it["id"])] = value
                names[str(it["id"])] = f"{_label(it)} ({entry['name'] or entry['title']})"
    return fills, names, inside


def plan(items: list[dict], profile: dict, resume: str | None, page_text: str = "") -> tuple[dict, dict, dict]:
    """What to put where: ({id: value} for browser fields, {id: file} for uploads, {id: name} of what gets filled)."""
    fields = [it for it in items if _field(it)]
    fills, names, inside = history_plan(items)
    has_code = any(_CODE_FIELD.search(_text(it)) and re.search(r"\+?\d{1,3}", str(it.get("value") or "")) for it in items)
    uploads = {}
    job, used = latest_job(), set()
    target = _resume_input(items, page_text)
    if target is not None and resume and _empty(target):
        uploads[target["id"]] = resume
    for it in fields:
        if it["id"] in inside or str(it["id"]) in fills:
            continue                      # employment blocks are filled from the work history above
        wrong = not _empty(it) and bool(B.shape_problem(it, it.get("value")))   # e.g. a city typed into "Full name"
        if _choice(it) or (not _empty(it) and not wrong):
            continue
        if it.get("type") == "file" or (it.get("tag") == "button" and _RESUME.search(_text(it))):
            continue
        if (it.get("type") or "") not in _TEXT_TYPES and it.get("tag") == "input":
            continue
        if answers.is_secret_question(_text(it) + " " + str(it.get("question") or "")):
            continue                      # passwords and codes: only browser_type_secret, from the vault
        value = _value_for(it, profile)
        if value is None:
            value = _job_value(it, job, used)
        if value is None:
            continue
        if has_code and _PHONE_ONLY.search(_text(it)):
            value = re.sub(r"^\s*(\+|00)\d{1,3}[\s-]*", "", value)   # the country code has its own field (Workday)
        if (it.get("role") == "combobox" or B.wants_pick(it)) and len(value) > 40:
            continue                      # a sentence the user wrote elsewhere is not one of this list's options
        if it.get("tag") == "select":
            picked = pick_option(it.get("options") or [], value)
            if picked is None and re.search(r"location|country|where", _text(it), re.I):
                picked = pick_option(it.get("options") or [], country_of(profile) or "")   # a list of countries
            value = picked
            if value is None:
                continue
        if B.answer_problem(it, value) or B.captcha_problem(it):
            continue
        fills[str(it["id"])] = value
        names[str(it["id"])] = _label(it)
    groups: dict[str, list[dict]] = {}
    for it in fields:
        if _choice(it) and it.get("question"):
            groups.setdefault(it["question"], []).append(it)
    for question, options in groups.items():
        if len(options) < 2 or any(o.get("checked") for o in options) or B.captcha_problem(options[0]):
            continue
        answer = _known_answer(question)
        labels = [o.get("label") or "" for o in options]
        if answer is None and answers.classify(question) in ("gender", "demographics"):
            declines = [label for label in labels if answers.DECLINE.search(label)]
            chosen = declines[0] if len(declines) == 1 else None   # declining is always a truthful answer
        else:
            chosen = pick_option(labels, answer) if answer else None
        option = next((o for o in options if (o.get("label") or "") == chosen), None) if chosen else None
        if option is None:
            continue
        value = option.get("label") if option.get("tag") == "button" else "true"
        if B.answer_problem(option, value):
            continue
        fills[str(option["id"])] = value
        names[str(option["id"])] = f"{answers.short(question)}: {chosen}"
    return fills, uploads, names


def still_open(backend) -> list[str]:
    """Required fields that are still empty, as snapshot lines with what to do about each."""
    fields = [it for it in backend.items.values() if _field(it)]
    if not fields:
        return []
    try:
        check = backend.call(backend.form_check, fields[0]["id"]) or {}
    except Exception:  # noqa: BLE001 - the page's own required markers still count
        check = {}
    wanted = {_norm(q).replace(" ", "") for q in check.get("empty") or []}
    from . import work_history as W
    history = {it["id"] for b in W.blocks(fields) if W.is_history_block(b)
               for _, it in ([("anchor", b["anchor"])] if b.get("anchor") else []) + b["fields"]}
    lines, seen = [], set()
    for it in fields:
        question = it.get("question") if _choice(it) else (it.get("label") or it.get("question"))
        key = _norm(question).replace(" ", "")
        flagged = key in wanted or any(key and (key in w or w in key) for w in wanted if len(w) > 3)
        if not (it.get("required") or flagged) or not question:
            continue
        if _choice(it):
            group = [o for o in fields if _choice(o) and o.get("question") == it.get("question")]
            if any(o.get("checked") for o in group) or question in seen:
                continue
            seen.add(question)
        elif not _empty(it):
            continue
        if it["id"] in history:
            how = ("the user's own dates and places: call ask_job_dates if Karya doesn't know them (then they're "
                   "filled on every form)" if W.date_role(it) or W.field_role(it) in ("location", "current") else
                   "this job/school block: fill it from the resume (work_history shows what Karya knows)")
        else:
            how = ("the user's own answer: ask_user" if answers.classify(question) else
                   "answer it from the resume and the job" if not _choice(it) else "choose the true option")
        place = bool(re.search(r"location|city|town|address|country|region", f"{it.get('label', '')} {it.get('name', '')}",
                               re.I))
        if it.get("role") == "combobox" and not place and it["id"] not in history and not it.get("options") \
                and len(lines) < 25 and hasattr(backend, "options_of") and \
                sum(1 for line in lines if "options=" in line) < 6:
            try:
                found = backend.call(backend.options_of, it["id"]) or []
            except Exception:  # noqa: BLE001 - the AI can still open it
                found = []
            if found:
                it["options"] = found
        line = B._fmt(it)
        more = (it.get("options") or [])[12:30]
        if more:
            line += " | more options: " + "|".join(more)
        lines.append(f"  {line}  <- {how}")
    return lines[:25]


def run(resume_path: str = "") -> str:
    """Fill the form on screen; '' when the page has no form fields."""
    from . import jobs
    backend = B._current()
    snap = B._run("snapshot", None, 150)
    if snap.startswith(("ERROR", "NOT DONE")):
        return f"AUTOFILL: couldn't read the page ({snap[:200]})"
    items = list(backend.items.values())
    if not any(_field(it) and not _choice(it) for it in items):
        return ""
    job = _job_here(backend.url) or {}
    resume = resume_path if resume_path and Path(resume_path).exists() else resume_for(job.get("company") or "")
    page_text = snap.split("Page text", 1)[-1][:3000] if "Page text" in snap else ""
    fills, uploads, names = plan(items, jobs.get_application_profile(), resume, page_text)
    report, problems = [], []
    planned = {str(k): v for k, v in fills.items()}
    before = {str(it["id"]): it for it in items}
    if fills:
        out = B._run("fill_many", fills)
        head = out.split("\nURL:")[0]
        problems += re.findall(r"Problems: (.+)", head)
        if not head.startswith("Filled"):
            problems.append(head[:200])
    for element_id, path in uploads.items():
        stop = B._upload_precheck({"file_path": path, "element_id": element_id})
        out = stop or B._run("upload", element_id, path)
        if not out.startswith("Uploaded"):
            problems.append(out.split("\n")[0][:200])
    if fills or uploads:
        B._run("snapshot", None, 150)     # the form as it is now: count only what really took
    now = backend.items
    done, missed = [], []
    for element_id, name in names.items():
        old = before.get(str(element_id)) or {}
        new = now.get(int(element_id)) or next((it for it in now.values() if it.get("label") == old.get("label")
                                                and it.get("question") == old.get("question")), None)
        if not _choice(old):
            took = new is not None and not _empty(new)
        elif new is not None and "checked" in new:
            took = bool(new["checked"])
        else:
            took = True                   # a Yes/No button that doesn't show a pressed state: it was clicked
        (done if took else missed).append(name)
    for element_id, path in uploads.items():
        new = now.get(int(element_id))
        if new is None or not _empty(new):
            done.append(f"resume {Path(path).name}")
        else:
            missed.append("resume")
    if not fills and not uploads:
        report.append("AUTOFILL: nothing Karya knows was missing on this page.")
    else:
        report.append(f"AUTOFILL: filled {len(done)} field(s) in one step: " + ("; ".join(done) or "none") + ".")
    if missed:
        report.append("Didn't take (no matching option, or the page rejected it): " + "; ".join(missed) + ".")
    if problems:
        report.append("Problems: " + " | ".join(problems)[:600])
    target = _resume_input(list(backend.items.values()), page_text)
    resume_open = target is not None and _empty(target) and not uploads
    if resume_open:
        report.append("Resume: no resume tailored for this job yet. Run tailor_resume for it, then browser_upload the "
                      "PDF (or call apply_autofill again).")
    left = still_open(backend)
    if left:
        report.append(f"Still open ({len(left)}):\n" + "\n".join(left))
        report.append("NEXT: answer the open ones in ONE browser_fill (one ask_user first for the personal ones), then "
                      "click the form's Next/Submit.")
    else:
        report.append("NEXT: every required field is filled. Click the form's Next/Submit.")
    return "\n".join(report)


@tool("apply_autofill", "Fill the job application form on screen in ONE step with everything Karya already knows: "
      "name, email, phone, LinkedIn/GitHub/portfolio, location, the resume tailored for this job, and every question "
      "the user answered before (notice period, salary, gender, experience...). Never guesses, never submits. Returns "
      "what it filled and the questions still open. It runs by itself when you open a job the user picked; call it "
      "again on each new page of a multi-page form.", {
    "resume_path": P("string", "Resume PDF to attach (default: the one tailored for this job)"),
}, group="jobs")
def apply_autofill(resume_path: str = ""):
    return run(resume_path) or "AUTOFILL: this page has no form fields. Click its Apply button first."


def after_open(url: str) -> str:
    """Autofill when the page belongs to a job the user picked (opened, Apply clicked, next form page)."""
    from .. import apply_queue
    job = apply_queue.job_for_page(url)
    if not job or job.get("status") != "pending":
        return ""
    try:
        note = run()
    except Exception as exc:  # noqa: BLE001 - autofill is a shortcut; the AI can still fill the form
        return f"\n\nAUTOFILL didn't run ({type(exc).__name__}). Fill the form with browser_fill."
    return f"\n\n{note}" if note else ""
