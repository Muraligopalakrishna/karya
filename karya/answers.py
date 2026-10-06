"""Answers only the user can give on a form: notice period, current/expected salary, gender, years of a specific
experience, visa/sponsorship, relocation, date of birth.

Karya never lets the AI guess these. A value is typed into such a field only if it matches what the user told Karya;
otherwise the AI has to ask the user first (ask_user). Answers are saved in the profile and reused next time."""
from __future__ import annotations

import re

from .memory import memory_store

# category -> (general profile field or None, pattern on the question text)
CATEGORIES: dict[str, tuple[str | None, str]] = {
    # a past job's dates ("Start date year", "End date month"). Checked first: they are NOT "when can you start", and
    # treating them so blocked real dates and overwrote the user's notice period with a month name (2026-10-06).
    "history_dates": (None, r"\b(start|end|starting|ending)\s+(date\s+)?(year|month|day)\b|\b(year|month)\s+(of\s+)?"
                            r"(start|end)\b|^\W*end\s+date\b|\bdate\s+(started|ended|left)\b|"
                            r"\b(employment|job|role|position|internship)\s+(start|end)\s+date\b"),
    "notice_period": ("notice_period", r"notice period|when can you (start|join)|earliest (start|joining|possible)|joining (date|time)|"
                                        r"available to (start|join)|how soon can you|start date|availability to (start|join)"),
    "current_salary": ("current_salary", r"current (ctc|salary|compensation|comp\b|pay\b|package|fixed|base|annual)|"
                                          r"present (ctc|salary|comp)|last drawn|current total compensation"),
    "expected_salary": ("expected_salary", r"expected (ctc|salary|compensation|comp\b|pay\b|package|annual|fixed)|"
                                            r"salary expectations?|desired (salary|pay|compensation)|compensation expectations?|"
                                            r"salary requirements?|fixed comp(ensation)? below which"),
    "gender": ("gender", r"\bgender\b|\bsex\b|\bpronouns?\b"),
    "demographics": (None, r"ethnicit|\brace\b|veteran|disabilit|sexual orientation|\bcaste\b|religio|transgender|hispanic|latin[oax]"),
    "experience_years": (None, r"how many years|years of (relevant |professional |total |work |full[- ]time |hands[- ]on )?experience|"
                               r"\btotal experience|\b(at least|minimum( of)?|min\.?|over|more than) \d+\+? ?(years|yrs)|\d+\+? ?(years|yrs) of"),
    "work_authorization": ("work_authorization", r"\bvisa\b|sponsor|work (permit|authori[sz]ation)|authori[sz]ed to work|"
                                                 r"right to work|citizenship|legally (eligible|allowed|authori[sz]ed)"),
    "relocation": ("relocation", r"relocat|willing to (move|work from|commute)|open to (working )?(on-?site|from (the )?office|in[- ]office|hybrid)|"
                                 r"work from (the )?office|comfortable (working|with|commuting)|commut(e|ing) to"),
    "date_of_birth": ("date_of_birth", r"date of birth|\bdob\b|\bage\b|birth ?date"),
    "location": ("location", r"^\s*(current |your |present )?(location|city|town)\b|\b(which|what) city\b|where (are you|do you) "
                             r"(based|live|located)|current(ly)? (based|located|living)|(city|place) of residence"),
}
COUNTRIES = {"india", "usa", "us", "united states", "united states of america", "uk", "united kingdom", "canada", "germany",
             "singapore", "uae", "united arab emirates", "australia", "netherlands", "ireland", "france", "japan", "remote"}
_COMPILED = {name: re.compile(pattern, re.I) for name, (_, pattern) in CATEGORIES.items()}
DECLINE = re.compile(r"prefer not|decline|don'?t wish|do not wish|not to (say|disclose|answer)|rather not", re.I)
RECENT_USER: list[str] = []      # the user's last few chat messages (set by the agent)


def note_user_message(text: str) -> None:
    RECENT_USER.append(str(text or "")[:2000])
    del RECENT_USER[:-4]


def said_by_user(value: str) -> bool:
    """The value comes from what the user recently wrote in the chat: all its numbers, and most of its words."""
    said = " ".join(RECENT_USER).lower().replace(",", "")
    text = str(value or "").lower().replace(",", "")
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    if any(n not in said for n in numbers):
        return False
    filler = {"the", "and", "for", "not", "yes", "days", "day", "lpa", "inr", "per", "annum", "month", "months", "weeks",
              "week", "with", "from", "other", "countries", "jobs", "job", "needed", "need", "needs", "required"}
    words = [w for w in re.findall(r"[a-z]{3,}", text) if w not in filler]
    if not words:
        return bool(numbers)  # a bare "Yes"/"No" is never inferred from the chat; ask instead
    return sum(w in said for w in words) / len(words) >= 0.6


SENSITIVE_FIELDS = {field for field, _ in CATEGORIES.values() if field} | {"needs_visa_sponsorship", "years_experience"}
# Words that show a chat message is about that topic (so "30" from "apply to 30 jobs" isn't taken as a notice period).
TOPIC_WORDS = {
    "notice_period": r"notice|join|start|immediate", "current_salary": r"current|ctc|salary|lpa|lakh|package|earn|stipend",
    "expected_salary": r"expect|ctc|salary|lpa|lakh|package", "gender": r"\b(male|female|man|woman|gender|non-?binary)\b",
    "location": r"\b(city|location|live|living|based|from|stay|staying|in)\b", "relocation": r"relocat|move|shift",
    "work_authorization": r"visa|sponsor|authori", "experience_years": r"year|yrs|experience",
    "date_of_birth": r"born|birth|dob|\bage\b", "demographics": r"caste|religion|ethnic|race|veteran|disab",
}


def said_for(category: str, value: str) -> bool:
    """The user gave this answer in the chat, in a message about that topic."""
    topic = TOPIC_WORDS.get(category)
    return bool(topic and re.search(topic, " ".join(RECENT_USER), re.I) and said_by_user(value))
_STOP = {"the", "you", "your", "are", "and", "for", "with", "have", "what", "how", "any", "this", "that", "our", "please",
         "required", "do", "of", "to", "in", "a", "an", "is", "be", "if", "or", "on", "at", "as", "we", "us"}


def classify(question: str) -> str | None:
    text = question or ""
    for name, pattern in _COMPILED.items():
        if pattern.search(text):
            return name
    return None


def _norm(text: str) -> str:
    text = re.sub(r"[✱*]|\(required\)|\brequired\b", " ", str(text or ""), flags=re.I)
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if len(w) > 1 and w not in _STOP}


def short(question: str) -> str:
    q = re.sub(r"\s*[✱*]\s*$", "", str(question or "")).strip()
    return q[:90] + ("..." if len(q) > 90 else "")


def saved_answers() -> dict[str, str]:
    prof = memory_store.load().get("profile", {})
    answers = prof.get("screening_answers")
    return {k: v for k, v in answers.items() if not is_secret_question(k)} if isinstance(answers, dict) else {}


# A password typed into a question card was once saved here in plain text (2026-10-06). These are never stored or used.
_SECRET_Q = re.compile(r"pass ?(word|code|phrase)|\bpin\b|\botp\b|one[- ]time (code|password)|verification code|"
                       r"security (question|answer|code)|\bsecret\b|\bcvv\b|card number", re.I)


def is_secret_question(question: str) -> bool:
    return bool(_SECRET_Q.search(str(question or "")))


def saved_answer(question: str) -> str | None:
    """The user's answer to this question: their latest general answer for facts like notice period or location,
    otherwise their answer to this (or a very similar) question."""
    prof = memory_store.load().get("profile", {})
    category = classify(question)
    field = CATEGORIES[category][0] if category else None
    if field and prof.get(field) not in (None, ""):
        return str(prof[field])
    if is_secret_question(question):
        return None
    answers = saved_answers()
    key = _norm(question)
    if key in answers:
        return str(answers[key])
    words = _words(question)
    best, best_score = None, 0.0
    for other, value in answers.items():
        theirs = _words(other)
        score = len(words & theirs) / max(1, len(words | theirs))
        if score > best_score:
            best, best_score = value, score
    if best is not None and best_score >= 0.75:
        return str(best)
    return None


def _same(value: str, saved: str) -> bool:
    a, b = _norm(value), _norm(saved)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return len(b) >= 4 and (b in a or a in b)


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
           "december")
_ONGOING = re.compile(r"^\W*(present|current(ly)?|now|ongoing|till (date|now)|to date|still (working|there))\W*$", re.I)


def _known_facts() -> str:
    """Everything the user really said about themselves: the resume, their saved answers, their recent messages."""
    try:
        from .tools.jobs import read_resume  # lazy: jobs imports this module
        resume = str(read_resume() or "")
    except Exception:  # noqa: BLE001 - no resume: only the answers count
        resume = ""
    if resume.startswith("ERROR"):
        resume = ""
    return " ".join([resume, *map(str, saved_answers().values()), *RECENT_USER]).lower()


def _in_known_facts(value: str) -> bool:
    """A job date may be typed only if its year and month appear in what the user gave Karya."""
    facts, v = _known_facts(), value.strip().lower()
    if _ONGOING.match(v):
        return bool(re.search(r"\b(present|current|ongoing|till date|to date)\b", facts))
    for year in re.findall(r"\b(?:19|20)\d{2}\b", v):
        if year not in facts and not re.search(rf"[-–—’'/]\s?{year[2:]}\b", facts):
            return False
    for number, month in enumerate(_MONTHS, 1):
        if re.search(rf"\b{month[:3]}", v) or v in (str(number), f"{number:02d}"):
            return bool(re.search(rf"\b{month[:3]}", facts))
    return True


def check(question: str, value) -> str | None:
    """None if the value may be typed; otherwise why not (for the AI)."""
    category = classify(question)
    if not category:
        return None
    text = str(value if value is not None else "").strip()
    if not text:
        return None  # clearing a field claims nothing
    if category == "history_dates":
        if _in_known_facts(text):
            return None
        return (f'"{short(question)}": "{text[:40]}" isn\'t in the user\'s resume or their answers, so it would be a '
                "guess. Ask the user for the start and end dates of each job (one ask_user for all of them)")
    if category in ("gender", "demographics") and DECLINE.search(text):
        return None  # "prefer not to say" is always a truthful answer
    saved = saved_answer(question)
    if saved is None:
        if said_for(category, text):
            save(question, text)  # the user said it in the chat: remember it for the next forms too
            return None
        return f'"{short(question)}" needs the user\'s own answer'
    if category in ("work_authorization", "relocation"):
        return None  # the user's own rule (e.g. "no sponsorship in India, yes elsewhere") applied to this job
    if category == "location":
        mine, theirs = _words(text), _words(saved)
        if mine and mine <= theirs:
            return None  # the same place, or less detail ("India" for "Hyderabad, India")
        if theirs and theirs <= mine and _norm(saved) not in COUNTRIES:
            return None  # more detail around the user's own city ("Hyderabad, Telangana, India")
        if _norm(saved) in COUNTRIES:
            return f'"{short(question)}": the user only told Karya "{saved}", so which city is a guess. Ask the user'
        return f'"{short(question)}": the user\'s location is "{saved}", not "{text[:80]}"'
    if _same(text, saved):
        return None
    return f'"{short(question)}": the user\'s answer is "{saved}", not "{text[:80]}"'


def save(question: str, answer: str) -> None:
    """Remember the user's answer for this question, and as their general answer for facts like notice period."""
    answer = str(answer or "").strip()[:300]
    if not answer or is_secret_question(question):
        return  # passwords, PINs and codes belong in the vault (encrypted), never in the profile
    data = memory_store.load()
    prof = data.setdefault("profile", {})
    answers = prof.get("screening_answers") if isinstance(prof.get("screening_answers"), dict) else {}
    answers[_norm(question)] = answer
    prof["screening_answers"] = dict(list(answers.items())[-60:])
    category = classify(question)
    field = CATEGORIES[category][0] if category else None
    if field:
        prof[field] = answer
    memory_store.save(data)


def normalize_questions(raw) -> list[dict]:
    """ask_user accepts ["Notice period?"] or [{"question": "...", "options": [...]}] (models write both)."""
    if isinstance(raw, str):
        raw = [q for q in re.split(r"\n|;", raw) if q.strip()]
    out, seen = [], set()
    for entry in raw or []:
        if isinstance(entry, dict):
            question = str(entry.get("question") or entry.get("q") or entry.get("label") or "").strip()
            options = [str(o).strip()[:80] for o in (entry.get("options") or []) if str(o).strip()][:15]
            hint = str(entry.get("hint") or entry.get("placeholder") or entry.get("example") or "").strip()[:80]
        else:
            question, options, hint = str(entry).strip(), [], ""
        if question and _norm(question) not in seen:
            seen.add(_norm(question))
            out.append({"q": question[:200], "options": options, **({"hint": hint} if hint else {})})
    return out[:10]


# Asked once, at the start of an application run, so Karya doesn't stop at every form for them.
BASICS = [
    ("What is your notice period?", ["Immediately", "15 days", "30 days", "60 days", "90 days"], "e.g. Immediately"),
    ("What is your current CTC (salary per year)?", ["0 (fresher, no salary yet)"], "e.g. 4 LPA, or 0 if none"),
    ("What is your expected CTC (salary per year)?", [], "e.g. 8 LPA"),
    ("Are you willing to relocate for a job (e.g. to Bangalore)?", ["Yes, anywhere in India", "Yes, within India",
                                                                    "No, only my current city"], ""),
    ("Which city do you live in?", [], "e.g. Hyderabad"),
    ("Gender", ["Male", "Female", "Prefer not to say"], ""),
]


def missing_basics() -> list[dict]:
    out = []
    for question, options, hint in BASICS:
        saved = saved_answer(question)
        if saved is None or (classify(question) == "location" and _norm(saved) in COUNTRIES):
            out.append({"q": question, "options": options, "hint": hint, "value": ""})
    return out
