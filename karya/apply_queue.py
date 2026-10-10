"""The jobs the user picked in the pick list, kept by Karya itself instead of the AI's memory.

A job is marked applied when a Submit on its page is confirmed (RESULT: SUBMITTED) - Karya then records it in the
tracker too. If the AI tries to finish while picked jobs are left, the agent tells it to continue with the next one."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlparse

from .config import DATA_DIR

QUEUE_FILE = DATA_DIR / "cache" / "apply_queue.json"
DONE = ("applied", "skipped", "failed")
MAX_AGE_SECONDS = 3 * 24 * 3600   # an old pick list doesn't steer new tasks


def queue_file():
    """The chat's pick list, or a bot's own (a bot's job search never replaces the user's picks)."""
    from .runctx import current
    run = current()
    return QUEUE_FILE.with_name(f"apply_queue.{run.agent_id}.json") if run.is_bot else QUEUE_FILE


def _read() -> dict:
    try:
        data = json.loads(queue_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load() -> list[dict]:
    jobs = _read().get("jobs")
    return jobs if isinstance(jobs, list) else []


def age_seconds() -> float:
    return time.time() - float(_read().get("created") or 0)


def _save(jobs: list[dict], created: float | None = None) -> None:
    queue_file().parent.mkdir(parents=True, exist_ok=True)
    old = _read()
    data = {"created": created or old.get("created") or time.time(), "jobs": jobs,
            "auto_submit": bool(old.get("auto_submit")) if created is None else False}
    queue_file().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def start(picked: list[dict], auto_submit: bool = False) -> list[dict]:
    """A new pick list replaces the old one. auto_submit: the user pre-approved submitting these jobs."""
    jobs = [{"id": str(j.get("id")), "title": j.get("title") or "", "company": j.get("company") or "",
             "url": j.get("url") or "", "apply_via": j.get("apply_via") or "", "status": "pending", "note": ""}
            for j in picked if j.get("id")]
    _save(jobs, created=time.time())
    set_auto_submit(auto_submit)
    return jobs


def auto_submit_on() -> bool:
    data = _read()
    return bool(data.get("auto_submit")) and age_seconds() <= MAX_AGE_SECONDS


def set_auto_submit(on: bool) -> None:
    data = _read()
    if data:
        data["auto_submit"] = bool(on)
        queue_file().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _update(job_id: str, **fields) -> dict | None:
    jobs = load()
    for job in jobs:
        if job.get("id", "").upper() == str(job_id).strip().upper():
            job.update(fields)
            _save(jobs)
            return job
    return None


def bump_try(job_id: str) -> None:
    job = next((j for j in load() if j.get("id", "").upper() == str(job_id).upper()), None)
    if job is not None:
        _update(job_id, auto_tries=int(job.get("auto_tries") or 0) + 1)


def note_result(url: str, result: str) -> None:
    """Remember an unclear Submit result: Karya won't submit that job again without asking."""
    job = job_for_page(url)
    if job:
        _update(job["id"], last_result=result)


def pending() -> list[dict]:
    if age_seconds() > MAX_AGE_SECONDS:
        return []
    return [j for j in load() if j.get("status") == "pending"]


def mark(job_id: str, status: str, note: str = "") -> dict | None:
    jobs = load()
    for job in jobs:
        if job.get("id", "").upper() == str(job_id).strip().upper():
            job["status"], job["note"], job["updated"] = status, str(note)[:200], time.strftime("%Y-%m-%d %H:%M")
            _save(jobs)
            return job
    return None


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def posting_key(url: str) -> str:
    """The id part of a job URL, used to recognise its pages: a query id (LinkedIn currentJobId, Greenhouse gh_jid),
    a Lever/Ashby uuid, a long number (LinkedIn .../associate-product-manager-at-navi-4472516899 and .../view/4472516899/)."""
    parsed = urlparse(url or "")
    for key in ("currentJobId", "gh_jid", "jobId", "job_id"):
        value = (parse_qs(parsed.query).get(key) or [""])[0]
        if len(value) >= 5:
            return value
    found = _UUID.search(parsed.path)
    if found:
        return found.group(0)
    numbers = re.findall(r"\d{6,}", parsed.path)
    if numbers:
        return max(numbers, key=len)
    segments = [s for s in parsed.path.split("/") if len(s) >= 5 and sum(c.isdigit() for c in s) >= 3]
    return max(segments, key=len) if segments else ""


def find_by_url(*urls: str, jobs: list[dict] | None = None) -> dict | None:
    for job in load() if jobs is None else jobs:
        key = posting_key(job.get("url", ""))
        if key and any(key in (u or "") for u in urls):
            return job
    return None


# The picked job Karya is applying to right now. Its form often lives on another address than the posting:
# SmartRecruiters' "I'm interested" opens /oneclick-ui/... without the posting id, Workday adds /apply/..., so the
# form, its autofill and its Submit are tied to the job by the site and the time instead.
_CURRENT_BY_RUN: dict[str, dict] = {}   # the job being applied to right now, per run (the chat and each bot)


def _cur() -> dict:
    from .runctx import current
    return _CURRENT_BY_RUN.setdefault(current().id, {})
CURRENT_SECONDS = 45 * 60


_SHARED_HOSTS = re.compile(r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com|workable\.com|"
                           r"recruitee\.com|jobvite\.com|breezy\.hr|bamboohr\.com|teamtailor\.com|"
                           r"personio\.(de|com)|keka\.com|darwinbox\.in|cutshort\.io|instahyre\.com)$")


def _site(url: str) -> str:
    """The site an application lives on. Hosts shared by many companies (Greenhouse, Lever, SmartRecruiters...) also
    need the company's own part of the address, so another company's job there isn't taken for this one."""
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if not _SHARED_HOSTS.search(host):
        return host
    parts = [p.lower() for p in parsed.path.split("/") if p]
    if parts[:2] == ["oneclick-ui", "company"] and len(parts) > 2:
        return f"{host}/{parts[2]}"           # SmartRecruiters' form: /oneclick-ui/company/<company>/...
    if host.endswith(("cutshort.io", "instahyre.com")):
        return host                            # one profile and form per site, whatever the company
    return f"{host}/{parts[0]}" if parts else host


def set_current(job: dict | None, url: str = "") -> None:
    if not job:
        return
    _cur().clear()
    _cur().update(id=job.get("id"), site=_site(url or job.get("url", "")), time=time.time())


def current_for(url: str) -> dict | None:
    """The job being applied to, when this page is on the same site and the application started recently."""
    if not _cur() or time.time() - float(_cur().get("time") or 0) > CURRENT_SECONDS:
        return None
    site = _site(url)
    if not site or site != _cur().get("site"):
        return None
    job = next((j for j in load() if j.get("id") == _cur().get("id")), None)
    if job is not None and job.get("status") == "pending":
        _cur()["time"] = time.time()
        return job
    return None


def job_for_page(*urls: str) -> dict | None:
    """The picked job a page belongs to: by its posting id, else the job being applied to on that site."""
    found = find_by_url(*urls)
    if found:
        return found
    for url in urls:
        job = current_for(url)
        if job:
            return job
    return None


def counts() -> dict[str, int]:
    out = {"pending": 0, "applied": 0, "skipped": 0, "failed": 0}
    for job in load():
        out[job.get("status", "pending")] = out.get(job.get("status", "pending"), 0) + 1
    return out


def _label(job: dict) -> str:
    return f'{job["id"]} "{job["title"]}" at {job["company"]}'


def status_text() -> dict:
    jobs = load()
    left = [j for j in jobs if j.get("status") == "pending"]
    return {"jobs": [{k: j.get(k) for k in ("id", "title", "company", "status", "note", "url")} for j in jobs],
            "next": {k: left[0].get(k) for k in ("id", "title", "company", "url", "apply_via")} if left else None,
            "left": len(left)}


def nudge_text() -> str:
    left = pending()
    total = len(load())
    if not left:
        return "All picked jobs are done."
    nxt = left[0]
    rest = "; ".join(_label(j) for j in left[1:6])
    return (f"NOT FINISHED: the user picked {total} jobs to apply to and {len(left)} are still left. Don't stop, and don't "
            f"ask them whether to continue - they already chose these. Next: {_label(nxt)} ({nxt['url']}). Do it now: "
            "get_job_details(job_id) -> tailor_resume(job_id) -> browser_open the apply page -> browser_fill (use ask_user "
            "first for anything only the user knows) -> browser_upload the new PDF -> click Submit. The user already "
            "decided to apply: don't skip a job because of its experience requirements or suggested skills. Only if "
            "applying is impossible (a login you don't have, a closed posting, a broken form), call "
            "application_queue(action=\"skip\", job_id, reason) and go on with the next one." + (f" After it: {rest}." if rest else ""))


_DONE_WORDS = re.compile(r"\b(done|applied|did|finished|submitted|manually|myself|completed|already)\b", re.I)


def done_by_user(text: str) -> list[dict]:
    """Picked jobs the user says they already did ("SPOT DRAFT IS DONE", "I did LoansJagat manually"): marked
    applied so Karya never does them again."""
    if not _DONE_WORDS.search(text or ""):
        return []
    squashed = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    marked = []
    for job in load():
        name = re.sub(r"[^a-z0-9]", "", (job.get("company") or "").lower())
        if job.get("status") != "applied" and len(name) >= 4 and name in squashed:
            marked.append(mark(job["id"], "applied", "you applied yourself") or job)
    return marked


_ALREADY = re.compile(r"\bapplied\s+(\d+|an?)\s+(second|minute|hour|day|week|month)s?\s+ago\b|"
                      r"\byou(?:'ve| have)?\s+already\s+applied\b|\balready applied\b|"
                      r"\byour application (?:was )?(?:submitted|sent)\b", re.I)


def already_applied(url: str, page_text: str) -> dict | None:
    """The page of a picked job says it's already applied (LinkedIn "Applied 2 days ago"): mark it done."""
    job = find_by_url(url)
    if not job or job.get("status") != "pending":
        return None
    found = _ALREADY.search(page_text or "")
    if not found:
        return None
    return mark(job["id"], "applied", f"already applied (the page says \"{found.group(0)}\")")


def summary_for_user() -> str:
    jobs = load()
    if not jobs:
        return ""
    marks = {"applied": "\u2714", "skipped": "\u2716", "failed": "\u2716", "pending": "\u2026"}
    words = {"applied": "submitted (the site confirmed it)", "skipped": "skipped", "failed": "not submitted",
             "pending": "not done yet"}
    lines = [f'- {marks.get(j["status"], "-")} {j["company"]} \u2014 {j["title"]}: {words.get(j["status"], j["status"])}'
             + (f' ({j["note"]})' if j.get("note") and j["status"] != "applied" else "") for j in jobs]
    return "**Your picked jobs:**\n" + "\n".join(lines)


_APPLY_WORDS = re.compile(r"\b(apply|applying|application|applications)\b", re.I)
_JOB_WORDS = re.compile(r"\b(jobs?|roles?|picked|positions?|openings?|list|queue)\b", re.I)
_CONTINUE = re.compile(r"\b(continue|go on|go ahead|carry on|keep going|resume|proceed|next|nexty|rest|remaining|"
                       r"others|do it|yes|ok|okay|sure|finish|done|start|again)\b", re.I)
_STOP = re.compile(r"\b(stop|stopped|cancel|don'?t|do not|dont|skip (the )?rest|no more|pause|enough|why|who told|"
                   r"i told)\b", re.I)


def note_run(was_queue: bool) -> None:
    """Remember whether the last task was this job list, so a bare "continue" means the right thing."""
    data = _read()
    if data:
        data["last_run_was_queue"] = bool(was_queue)
        queue_file().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


_OTHER_TASK = re.compile(r"\b(find|search|look|e-?mails?|mails?|send|post|buy|buyers?|sell|build|write|check|analy[sz]e|"
                         r"research|make|create|fix|tell|show|explain|what|how|website|site)\b", re.I)


def wanted_in(text: str) -> bool:
    """Is this message about carrying on with the picked jobs? Yes for "apply to the rest" / "continue the jobs", or
    a short "continue / next / X is done, do next" right after a job-list run. Other tasks and complaints never are."""
    text = (text or "").strip()
    if not text or _STOP.search(text) or len(text) > 400 or _OTHER_TASK.search(text):
        return False
    if _APPLY_WORDS.search(text) or (_JOB_WORDS.search(text) and _CONTINUE.search(text)):
        return True
    return len(text) <= 80 and bool(_CONTINUE.search(text)) and bool(_read().get("last_run_was_queue"))
