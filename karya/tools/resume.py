"""Resume builder and per-application tailoring.

The user's "master" resume is kept as structured data (data/resume_master.json). For each application the agent writes a
tailored version (summary, skill order, bullet emphasis aligned with the posting) and build_resume renders it to a PDF
using the user's own Chrome. Any skill that is not already in the master resume needs the user's explicit OK, so
nothing is invented."""
from __future__ import annotations

import html
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..config import DATA_DIR, settings
from ..registry import CRITICAL, SAFE, P, tool

MASTER_FILE = DATA_DIR / "resume_master.json"
SCHEMA = ('{"name","headline","email","phone","location","links":[{"label","url"}],"summary","skills":["..."],'
          '"experience":[{"title","company","location","start","end","bullets":["..."]}],'
          '"projects":[{"name","url","description","tech":["..."],"bullets":["..."]}],'
          '"education":[{"degree","school","start","end","details"}],"certifications":["..."]}')


def load_master() -> dict | None:
    try:
        data = json.loads(MASTER_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except (OSError, json.JSONDecodeError):
        return None


def _as_dict(resume) -> dict:
    if isinstance(resume, str):
        resume = json.loads(resume)
    if not isinstance(resume, dict):
        raise ValueError("resume must be an object using the schema " + SCHEMA)
    return resume


def _skill_list(resume: dict) -> list[str]:
    skills = resume.get("skills") or []
    if isinstance(skills, dict):
        skills = [s for group in skills.values() for s in (group if isinstance(group, list) else [group])]
    if isinstance(skills, str):
        skills = [skills]
    out: list[str] = []
    for s in skills:
        out.extend(x.strip() for x in re.split(r"[,|;\n]", str(s)) if x.strip())
    return out


def _key(skill: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", unicodedata.normalize("NFKC", skill).lower())


TAILOR_RULES = ("Keep every fact true: same jobs, dates, projects and education; you may reorder, rephrase bullets with "
                "the posting's wording when it describes what the person really did, rewrite the summary for this role, "
                "and reorder skills (most relevant first). Never add jobs, numbers or skills that are not in the resume. "
                "Put skills the job asks for that are NOT in the resume only under \"suggested_skills\".")
NO_AI_WHY = ("Karya has no AI key of its own here, so you (the AI app using Karya) write it. Karya then checks every fact "
             "against the user's real resume and makes the PDF.")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def own_ai() -> bool:
    """Karya has an AI of its own to call. People who only use Karya from another AI app (MCP) may have none: then
    that app's AI writes the resume JSON, and Karya checks the facts and makes the PDF."""
    from .. import llm
    active = llm.ACTIVE
    return active is not None and hasattr(active, "complete") and bool(getattr(active, "providers", True))


def _own_ai_json(system: str, user: str) -> dict | None:
    """Karya's own AI, or None when there is none or it failed (no key, Ollama not running, no JSON back)."""
    if not own_ai():
        return None
    try:
        return _ai_json(system, user)
    except Exception:  # noqa: BLE001 - the AI app using Karya writes it instead
        return None


def _numbers(value) -> set[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {n.replace(",", "") for n in _NUMBER.findall(text or "")}


def _same(a, b) -> bool:
    x, y = _key(str(a or "")), _key(str(b or ""))
    return bool(x and y) and (x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x)))


def _pick(item: dict, pool, field: str, second: str = "") -> dict | None:
    same = [m for m in pool or [] if isinstance(m, dict) and _same(m.get(field), item.get(field))]
    if len(same) > 1 and second:
        same = [m for m in same if _same(m.get(second), item.get(second))] or same[:1]
    return same[0] if same else None


def keep_facts(tailored: dict, master: dict) -> tuple[dict, list[str]]:
    """A tailored resume may reorder and reword, but its facts come from the master resume: jobs, titles, dates,
    projects, certifications. A bullet or summary with a number the master doesn't have (an invented '40%' or
    '5 years') is dropped. Returns (resume, notes about what was changed)."""
    known = _numbers(master)
    honest = lambda text: _numbers(str(text or "")) <= known  # noqa: E731
    tailored = dict(tailored)
    for field in ("summary", "headline", "skills", "experience", "projects", "certifications"):
        if not tailored.get(field) and master.get(field):
            tailored[field] = master[field]          # a section the AI left out comes from the master
    out, notes = dict(tailored), []
    for field, name, second in (("experience", "company", "title"), ("projects", "name", "")):
        if field not in tailored:
            continue
        kept = []
        for item in tailored.get(field) or []:
            if not isinstance(item, dict):
                continue
            real = _pick(item, master.get(field), name, second)
            if not real:
                notes.append(f"left out {item.get(name) or 'an entry'}: not in the user's resume")
                continue
            bullets = [b for b in item.get("bullets") or [] if isinstance(b, str)]
            good = [b for b in bullets if honest(b)]
            if len(good) < len(bullets):
                notes.append(f"dropped {len(bullets) - len(good)} bullet(s) with numbers that aren't in the resume "
                             f"({real.get(name)})")
            fixed = {**real, "bullets": good or list(real.get("bullets") or [])}
            if field == "projects" and item.get("description") and honest(item["description"]):
                fixed["description"] = item["description"]
            kept.append(fixed)
        out[field] = kept or list(master.get(field) or [])
    if "certifications" in tailored:
        certs = [c for c in tailored.get("certifications") or [] if any(_same(c, m) for m in master.get("certifications") or [])]
        out["certifications"] = certs or list(master.get("certifications") or [])
    for field in ("summary", "headline"):
        if tailored.get(field) and not honest(tailored[field]):
            out[field] = master.get(field, "")
            notes.append(f"kept the original {field}: the new one had numbers that aren't in the resume")
    return out, notes


def _in_text(value, plain: str) -> bool:
    """Every real word of `value` is in the file's text ('GITAM University' fits 'GITAM (Deemed to be University)')."""
    words = [_key(w) for w in re.split(r"[^\w+#.]+", str(value or "")) if len(_key(w)) >= 3]
    whole = _key(str(value or ""))
    return bool(whole) and (all(w in plain for w in words) if words else whole in plain)


def _from_file_only(data: dict, text: str) -> tuple[dict, list[str]]:
    """A resume converted by an AI keeps only what the file really says: skills, jobs, projects and certifications
    that appear in the text, and bullets whose numbers are in it."""
    plain, nums = _key(text), _numbers(text)
    inside = lambda value: _in_text(value, plain)  # noqa: E731
    out, notes = dict(data), []
    skills = _skill_list(data)
    out["skills"] = [s for s in skills if inside(s)]
    if len(out["skills"]) < len(skills):
        notes.append("left out skills that aren't in the file: " + ", ".join(s for s in skills if not inside(s))[:200])
    for field, name in (("experience", "company"), ("projects", "name"), ("education", "")):
        items = [dict(x) for x in data.get(field) or [] if isinstance(x, dict)]
        good = [x for x in items if not name or inside(x.get(name))]
        if len(good) < len(items):
            notes.append(f"left out {field} that aren't in the file: "
                         + ", ".join(str(x.get(name)) for x in items if not inside(x.get(name)))[:200])
        for x in good:
            if isinstance(x.get("bullets"), list):
                before = len(x["bullets"])
                x["bullets"] = [b for b in x["bullets"] if isinstance(b, str) and _numbers(b) <= nums]
                if len(x["bullets"]) < before:
                    notes.append(f"left out {before - len(x['bullets'])} bullet(s) with numbers that aren't in the file "
                                 f"({x.get(name) or field})")
        if field in data:
            out[field] = good
    if "certifications" in data:
        out["certifications"] = [c for c in data.get("certifications") or [] if isinstance(c, str) and inside(c)]
    digits = re.sub(r"\D", "", text)
    for field in ("name", "email", "phone"):
        value = data.get(field)
        ok = (_key(str(value or "")) in plain if field == "email" else inside(value)) or (
            field == "phone" and len(re.sub(r"\D", "", str(value or ""))) >= 7 and re.sub(r"\D", "", str(value)) in digits)
        if value and not ok:
            out[field] = ""
            notes.append(f"left out the {field}: it isn't in the file")
    if data.get("summary") and not _numbers(data["summary"]) <= nums:
        out["summary"] = ""
        notes.append("left out the summary: it had numbers that aren't in the file")
    return out, notes


def new_skills(resume: dict) -> list[str]:
    """Skills listed in `resume` that the master resume doesn't contain anywhere (they need the user's OK)."""
    master = load_master() or {}
    known = {_key(s) for s in _skill_list(master)}
    master_text = _key(json.dumps(master, ensure_ascii=False))
    return [s for s in _skill_list(resume) if _key(s) and _key(s) not in known and _key(s) not in master_text]


@tool("get_resume_data", "Get the user's master resume as structured data, or learn that none exists yet.", group="resume")
def get_resume_data():
    master = load_master()
    if master:
        return {"exists": True, "resume": master}
    has_file = settings.resume_path.exists()
    return {"exists": False, "resume_file": str(settings.resume_path) if has_file else None,
            "next": ("read_resume, convert it to the schema below and save_resume_data." if has_file else
                     "No resume yet: ask the user if they have one (they can set RESUME_PATH in Settings) or build one "
                     "by asking short questions: role they want, work history, projects, education, skills, links. "
                     "Then save_resume_data and build_resume."), "schema": SCHEMA}


def _save_risk(args: dict) -> tuple[str, str]:
    resume = _as_dict(args.get("resume") or {})
    added = new_skills(resume) if load_master() else []
    if added:
        return CRITICAL, "Add these NEW skills to your master resume (only approve skills you really have):\n- " + "\n- ".join(added)
    return SAFE, f"Save master resume for {resume.get('name', 'the user')}"


@tool("save_resume_data", "Create or update the user's master resume (structured data, schema in get_resume_data). "
      "Only include facts the user gave you or that are in their resume file.", {
    "resume": P("object", "Resume object (see schema)"),
}, required=["resume"], risk=_save_risk, group="resume")
def save_resume_data(resume):
    data = _as_dict(resume)
    MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"saved": True, "skills": _skill_list(data), "next": "build_resume to make a PDF."}


def _e(text) -> str:
    return html.escape(str(text or ""))


def render_html(r: dict) -> str:
    links = " · ".join(f'<a href="{_e(l.get("url"))}">{_e(l.get("label") or l.get("url"))}</a>'
                       for l in r.get("links") or [] if isinstance(l, dict))
    contact = " · ".join(_e(x) for x in (r.get("email"), r.get("phone"), r.get("location")) if x)

    def bullets(items):
        items = [i for i in items or [] if i]
        return "<ul>" + "".join(f"<li>{_e(i)}</li>" for i in items) + "</ul>" if items else ""

    parts = [f"<h1>{_e(r.get('name'))}</h1>"]
    if r.get("headline"):
        parts.append(f"<div class='headline'>{_e(r['headline'])}</div>")
    parts.append(f"<div class='contact'>{contact}{' · ' if contact and links else ''}{links}</div>")
    if r.get("summary"):
        parts.append(f"<h2>Summary</h2><p>{_e(r['summary'])}</p>")
    if _skill_list(r):
        parts.append("<h2>Skills</h2><p>" + " · ".join(_e(s) for s in _skill_list(r)) + "</p>")
    if r.get("experience"):
        parts.append("<h2>Experience</h2>")
        for x in r["experience"]:
            dates = " – ".join(_e(d) for d in (x.get("start"), x.get("end")) if d)
            parts.append(f"<div class='item'><div class='row'><b>{_e(x.get('title'))}</b>"
                         f"{' — ' + _e(x.get('company')) if x.get('company') else ''}<span>{dates}</span></div>"
                         f"{bullets(x.get('bullets'))}</div>")
    if r.get("projects"):
        parts.append("<h2>Projects</h2>")
        for x in r["projects"]:
            url = f" <a href='{_e(x.get('url'))}'>{_e(x.get('url'))}</a>" if x.get("url") else ""
            tech = f"<div class='tech'>{_e(', '.join(x.get('tech') or []))}</div>" if x.get("tech") else ""
            desc = f"<p>{_e(x.get('description'))}</p>" if x.get("description") else ""
            parts.append(f"<div class='item'><div class='row'><b>{_e(x.get('name'))}</b>{url}</div>{desc}{tech}{bullets(x.get('bullets'))}</div>")
    if r.get("education"):
        parts.append("<h2>Education</h2>")
        for x in r["education"]:
            dates = " – ".join(_e(d) for d in (x.get("start"), x.get("end")) if d)
            parts.append(f"<div class='item'><div class='row'><b>{_e(x.get('degree'))}</b>"
                         f"{' — ' + _e(x.get('school')) if x.get('school') else ''}<span>{dates}</span></div>"
                         f"{'<p>' + _e(x.get('details')) + '</p>' if x.get('details') else ''}</div>")
    if r.get("certifications"):
        parts.append("<h2>Certifications</h2>" + bullets(r["certifications"]))
    css = ("body{font-family:'Segoe UI',Arial,sans-serif;color:#111;margin:0;font-size:10.5pt;line-height:1.38}"
           "h1{font-size:21pt;margin:0}h2{font-size:11pt;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #999;"
           "margin:13px 0 5px;padding-bottom:2px}.headline{font-size:11.5pt;color:#333;margin-top:2px}.contact{color:#333;margin:4px 0 2px}"
           "a{color:#1a4fb4;text-decoration:none}ul{margin:3px 0 0 17px;padding:0}li{margin:1px 0}p{margin:3px 0}"
           ".item{margin:5px 0 7px}.row{display:flex;justify-content:space-between;gap:10px}.row span{color:#444;white-space:nowrap}"
           ".tech{color:#444;font-style:italic}@page{size:A4;margin:14mm 15mm}")
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(r.get('name'))} - Resume</title><style>{css}</style></head><body>{''.join(parts)}</body></html>"


def _build_risk(args: dict) -> tuple[str, str]:
    try:
        resume = _as_dict(args.get("resume")) if args.get("resume") else (load_master() or {})
    except (ValueError, json.JSONDecodeError):
        return SAFE, "Build resume PDF"
    added = new_skills(resume) if load_master() else []
    target = args.get("job_title") or "general"
    if added:
        return CRITICAL, (f"Tailored resume for '{target}' adds skills that are NOT in your resume. Approve only if you "
                          "really have them (they'll also be saved to your master resume):\n- " + "\n- ".join(added))
    return SAFE, f"Build resume PDF ({target})"


@tool("build_resume", "Render a resume to PDF. Without 'resume' it builds the master resume. For applications pass a "
      "TAILORED copy: reorder skills and bullets to match the job, rewrite the summary for that role, use the posting's "
      "wording for things the user really did. Never invent experience; new skills need the user's approval.", {
    "resume": P("object", "Tailored resume object (same schema). Omit to use the master resume."),
    "job_title": P("string", "Job the resume is tailored for"),
    "company": P("string", "Company the resume is tailored for"),
}, risk=_build_risk, group="resume")
def build_resume(resume=None, job_title: str = "", company: str = ""):
    master = load_master()
    data = _as_dict(resume) if resume else master
    if not data:
        return "ERROR: no resume data yet. Use get_resume_data, then save_resume_data first."
    added = new_skills(data) if master else []
    if added and master:  # approved (the risk check asked the user) -> remember them in the master resume
        master["skills"] = _skill_list(master) + added
        MASTER_FILE.write_text(json.dumps(master, ensure_ascii=False, indent=1), encoding="utf-8")
    folder = settings.workspace / "resumes"
    folder.mkdir(parents=True, exist_ok=True)
    who = re.sub(r"[^\w]+", "_", data.get("name") or "resume").strip("_") or "resume"
    tag = re.sub(r"[^\w]+", "_", " ".join(x for x in (company, job_title) if x)).strip("_")[:50]
    stem = f"{who}_Resume" + (f"_{tag}" if tag else "") + "_" + time.strftime("%Y%m%d")
    html_path, pdf_path = folder / f"{stem}.html", folder / f"{stem}.pdf"
    html_path.write_text(render_html(data), encoding="utf-8")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = None
        for channel in (settings.browser_channel, "chrome", "msedge", None):
            try:
                browser = p.chromium.launch(channel=channel, headless=True) if channel else p.chromium.launch(headless=True)
                break
            except Exception:
                continue
        if browser is None:
            return f"ERROR: could not start Chrome/Edge to make the PDF. HTML saved: {html_path}"
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.pdf(path=str(pdf_path), format="A4", print_background=True)
        browser.close()
    out = {"pdf": str(pdf_path), "html": str(html_path), "use": "browser_upload this PDF, or attach it in send_email."}
    if added:
        out["added_skills"] = added
    if company or job_title:
        remember_tailored(pdf_path, company, job_title)
    return out


def _resume_index_file() -> Path:
    return settings.workspace / "resumes" / "index.json"


_INDEX_LOCK = threading.Lock()


def remember_tailored(pdf_path, company: str, job_title: str) -> None:
    with _INDEX_LOCK:
        path = _resume_index_file()
        try:
            index = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            index = {}
        index[str(Path(pdf_path).resolve()).lower()] = {"company": company, "job_title": job_title,
                                                        "made": time.strftime("%Y-%m-%d %H:%M")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def tailored_for(file_path) -> dict | None:
    """Which job a resume PDF was tailored for (None for the master resume or unknown files)."""
    try:
        index = json.loads(_resume_index_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return index.get(str(Path(file_path).resolve()).lower())



def _ai_json(system: str, user: str) -> dict:
    from .. import llm
    if llm.ACTIVE is None:
        raise RuntimeError("no AI client available")
    text = llm.ACTIVE.complete(system, user)
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("the AI didn't return resume data")
    return json.loads(match.group(0))


@tool("import_resume", "Create the master resume from the user's resume file (PDF/DOCX/TXT) in one step. "
      "Use when get_resume_data says none exists. If Karya has no AI of its own (you're using it from another AI app), "
      "it returns the resume text: convert it to the schema and call import_resume again with resume=<that JSON>.", {
    "path": P("string", "Resume file (default: the one in Settings)"),
    "resume": P("object", "Only when Karya asks for it: the resume as JSON, made from the file's text (facts only)"),
}, group="resume")
def import_resume(path: str | None = None, resume=None):
    from .jobs import read_resume
    text = read_resume(path)
    if text.startswith("ERROR"):
        return text + " Ask the user for their resume file, or build one with them by asking short questions."
    if resume is not None:
        data = _as_dict(resume)
    else:
        data = _own_ai_json("You convert resumes into JSON. Use ONLY facts written in the resume; never add anything. "
                            "Return only JSON with this shape: " + SCHEMA,
                            "Resume text:\n" + text[:9000])
    if data is None:
        return {"karya_needs": "the resume as JSON, made by you", "why": NO_AI_WHY,
                "rules": "Use ONLY facts written in the resume text; never add anything. Keep names, dates and numbers "
                         "exactly as written.",
                "schema": SCHEMA, "resume_text": text[:9000], "then": "call import_resume(resume=<the JSON>)"}
    data, notes = _from_file_only(_as_dict(data), text)
    if isinstance(data.get("name"), str) and data["name"].isupper():
        data["name"] = data["name"].title()
    MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    MASTER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    out = {"saved": True, "name": data.get("name"), "skills": _skill_list(data),
           "experience": [f"{x.get('title')} - {x.get('company')}" for x in data.get("experience") or []],
           "projects": [x.get("name") for x in data.get("projects") or []]}
    if notes:
        out["checked"] = notes
    return out


@tool("tailor_resume", "Make a resume PDF tailored to one job: rewrites the summary for that role and puts the most "
      "relevant skills, projects and bullets first, using only the user's real facts. Never adds skills; any skill "
      "the job wants that the user may have is returned in 'suggested_skills' for the user to confirm.", {
    "job_id": P("string", "Job id from find_jobs, e.g. J2 (preferred)"),
    "job_url": P("string", "Job posting URL (if there's no id)"),
    "job_title": P("string", "Job title"),
    "company": P("string", "Company"),
    "job_description": P("string", "Key requirements, if there's no id or URL"),
    "resume": P("object", "Only when Karya asks for it: the tailored resume JSON you wrote (same shape as the master "
                          "resume, plus \"suggested_skills\")"),
}, group="resume")
def tailor_resume(job_url: str = "", job_title: str = "", company: str = "", job_description: str = "", job_id: str = "",
                  resume=None):
    if resume is not None:          # written by the AI app using Karya: checked against the master like any other
        out = _tailor(job_url, job_title, company, job_description, job_id, resume=resume)
        prepare_next()
        return out
    if job_id and not job_description:
        with _PREP_LOCK:
            future = _PREPARED.get(_prep_key(job_id))
        if future is not None:
            try:
                ready = future.result(timeout=240)  # usually done already (made while the last job was being filled)
            except Exception:  # noqa: BLE001 - make it now instead
                ready = None
            if isinstance(ready, dict) and Path(ready.get("pdf", "")).exists():
                prepare_next()
                return ready
    out = _tailor(job_url, job_title, company, job_description, job_id)
    prepare_next()
    return out


# ---- resumes for the next picked jobs are made in the background while Karya fills the current form ----
_PREP_LOCK = threading.Lock()
_PREP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="karya-resume-prep")
_PREPARED: dict[str, Future] = {}


def _prep_key(job_id: str) -> str:
    try:
        stamp = MASTER_FILE.stat().st_mtime
    except OSError:
        stamp = 0
    return f"{str(job_id).strip().upper()}@{stamp}"


def _can_prepare() -> bool:
    """Only with a Kiro brain: it gets its own CLI process for background work. Small free plans would just hit
    their rate limits sooner."""
    from .. import llm
    providers = getattr(llm.ACTIVE, "providers", None) or []
    return bool(providers) and getattr(providers[0], "name", "") == "kiro"


def prepare_next(count: int = 3) -> None:
    from .. import apply_queue
    if not _can_prepare() or not load_master():
        return
    for job in apply_queue.pending()[:count]:
        key = _prep_key(job["id"])
        with _PREP_LOCK:
            if key not in _PREPARED:
                _PREPARED[key] = _PREP_POOL.submit(_prepare_one, job["id"])


def _prepare_one(job_id: str):
    from .. import llm
    llm.LANE.name = "bg"
    try:
        return _tailor(job_id=job_id)
    finally:
        llm.LANE.name = "main"


def _tailor(job_url: str = "", job_title: str = "", company: str = "", job_description: str = "", job_id: str = "",
            resume=None):
    master = load_master()
    if not master:
        return "ERROR: no master resume yet. Call import_resume first (or build one with the user)."
    if job_id:
        from .jobs import job_by_id
        job = job_by_id(job_id) or {}
        job_url = job_url or job.get("url", "")
        job_title = job_title or job.get("title", "")
        company = company or job.get("company", "")
    posting = job_description
    if job_url:
        from .jobs import get_job_details
        try:
            details = get_job_details(job_url)
        except Exception:  # noqa: BLE001 - fall back to whatever we know about the job
            details = None
        if isinstance(details, dict):
            job_title = job_title or details.get("title") or ""
            company = company or details.get("company") or ""
            posting = (details.get("description") or details.get("text") or "") + " " + json.dumps(details.get("criteria") or {})
    if not posting:
        return "ERROR: give job_url or job_description"
    if resume is not None:
        tailored = _as_dict(resume)
    else:
        tailored = _own_ai_json(
            "You tailor resumes to a job. " + TAILOR_RULES + " Return only JSON: the resume (same shape) plus "
            "\"suggested_skills\": [...].",
            f"JOB: {job_title} at {company}\n{posting[:3500]}\n\nRESUME JSON:\n{json.dumps(master, ensure_ascii=False)[:6000]}")
    if tailored is None:
        return {"karya_needs": "the tailored resume, written by you", "why": NO_AI_WHY, "rules": TAILOR_RULES,
                "job": {"title": job_title, "company": company, "posting": posting[:3500]}, "master_resume": master,
                "then": "call tailor_resume again with the same job arguments plus resume=<the tailored resume JSON, "
                        "with \"suggested_skills\": [...]>"}
    tailored = dict(tailored)
    suggested = [s for s in tailored.pop("suggested_skills", []) or [] if isinstance(s, str)]
    tailored, checked = keep_facts(tailored, master)
    sneaked = new_skills(tailored)
    if sneaked:  # never let new skills in silently
        keep = {_key(s) for s in _skill_list(master)}
        tailored["skills"] = [s for s in _skill_list(tailored) if _key(s) in keep] or _skill_list(master)
        suggested += sneaked
    for key in ("name", "email", "phone", "location", "links", "education"):
        if master.get(key):
            tailored[key] = master[key]  # contact details and education always come from the master
    out = build_resume(tailored, job_title, company)
    if isinstance(out, dict):
        out["summary"] = tailored.get("summary", "")[:400]
        out["top_skills"] = _skill_list(tailored)[:8]
        if checked:
            out["checked"] = checked
        if suggested:
            out["suggested_skills"] = sorted(set(suggested))[:10]
            out["next"] = ("Use this PDF now; it already fits the job with the user's real skills. suggested_skills are "
                           "optional extras: don't ask about them during applications and never skip a job for them. "
                           "(Only if the user asks to improve the resume: ask, then add_resume_skills.)")
    return out


@tool("add_resume_skills", "Add skills to the user's master resume. Only after the user said they have them; "
      "they also approve the card.", {
    "skills": P("array", "Skills to add", items={"type": "string"}),
}, required=["skills"], group="resume",
      risk=lambda a: (CRITICAL, "Add these skills to your resume (approve only if you really have them):\n- " +
                      "\n- ".join(str(s) for s in (a.get("skills") or []))))
def add_resume_skills(skills: list[str]):
    master = load_master()
    if not master:
        return "ERROR: no master resume yet."
    known = {_key(s) for s in _skill_list(master)}
    added = [s for s in skills if _key(s) and _key(s) not in known]
    master["skills"] = _skill_list(master) + added
    MASTER_FILE.write_text(json.dumps(master, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"added": added, "skills": master["skills"]}
