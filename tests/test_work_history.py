"""Job dates come only from the user, are asked once and fill every form; the job being applied to is tracked.

The user, 2026-10-07: "it is hallucinating, ask the user the start and end date and remember for the rest of the
applications". Karya had typed 03/2024, 01/2023 and 06/2022 into SmartRecruiters' experience blocks."""
import asyncio
import json

import pytest

from karya import apply_queue, kiro_bridge
from karya.tools import autofill, browser, resume
from karya.tools import work_history as W

MASTER = {"name": "Asha Rao", "experience": [
    {"title": "Founder & Product Lead", "company": "Acme Labs", "start": "", "end": "", "bullets": ["Built it"]},
    {"title": "Founder", "company": "Beta Learn", "start": "", "end": "", "bullets": ["Taught"]},
    {"title": "Video Intern", "company": "Gamma Studio", "start": "Jul 2024", "end": "Sep 2024", "bullets": ["Edited"]}],
    "education": [{"degree": "B.Tech, Computer Science", "school": "Delta University", "start": "2020", "end": "2024"}]}


@pytest.fixture()
def master(monkeypatch):
    resume.MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)
    resume.MASTER_FILE.write_text(json.dumps(MASTER), encoding="utf-8")
    return resume.MASTER_FILE


def _dates_given():
    return {"Founder & Product Lead at Acme Labs: when did you start? (month and year)": "march 2024",
            "Founder & Product Lead at Acme Labs: when did it end?": "Present (still doing it)",
            "Founder & Product Lead at Acme Labs: where was it? (city, or Remote)": "Hyderabad",
            "Founder at Beta Learn: when did you start? (month and year)": "06/2023",
            "Founder at Beta Learn: when did it end?": "Aug '25"}


def test_dates_as_people_write_them():
    assert W.parse_when("march 2024") == {"month": 3, "year": 2024, "present": False}
    assert W.parse_when("03/2024")["month"] == 3 and W.parse_when("2024-11")["month"] == 11
    assert W.parse_when("Aug '25") == {"month": 8, "year": 2025, "present": False}
    assert W.parse_when("Present (still doing it)")["present"] and W.parse_when("currently working")["present"]
    assert W.parse_when("2020") == {"month": None, "year": 2020, "present": False}
    assert W.parse_when("soon") is None and W.month_of("Mar") == 3 and W.month_of("09") == 9
    assert W.fmt_when(W.parse_when("march 2024")) == "Mar 2024"


def test_one_card_asks_only_whats_missing_and_saves_it_on_the_resume(master):
    asked = W.questions()
    qs = [q["q"] for q in asked]
    assert "Founder & Product Lead at Acme Labs: when did you start? (month and year)" in qs
    assert not any("Gamma Studio: when did" in q for q in qs)               # its dates are on the resume
    assert any("Gamma Studio: where was it" in q for q in qs)                # but not where it was
    assert not any("Delta University" in q for q in qs)                      # schools only when a form needs them
    saved = W.save_answers(asked, _dates_given())
    data = json.loads(master.read_text(encoding="utf-8"))
    acme, beta = data["experience"][0], data["experience"][1]
    assert (acme["start"], acme["end"], acme["location"]) == ("Mar 2024", "Present", "Hyderabad")
    assert (beta["start"], beta["end"]) == ("Jun 2023", "Aug 2025") and len(saved) == 5
    assert [q["q"] for q in W.questions()] == ["Founder at Beta Learn: where was it? (city, or Remote)",
                                               "Video Intern at Gamma Studio: where was it? (city, or Remote)"]


def _smartrecruiters_form():
    """Two experience blocks the site's resume parser created: title and company filled, dates empty."""
    rows = [("input", "text", "combobox", "Title*", "Founder & Product Lead", 100, 10),
            ("input", "text", "combobox", "Company", "Acme Labs", 100, 400),
            ("input", "text", "combobox", "Office location", "", 160, 10),
            ("input", "text", None, "From", "", 220, 10), ("input", "text", None, "To", "", 220, 400),
            ("input", "checkbox", None, "", None, 220, 700),
            ("input", "text", "combobox", "Title*", "Founder", 400, 10),
            ("input", "text", "combobox", "Company", "Beta Learn", 400, 400),
            ("input", "text", None, "From", "", 520, 10), ("input", "text", None, "To", "", 520, 400),
            ("input", "checkbox", None, "", None, 520, 700)]
    items = {}
    for n, (tag, kind, role, label, value, y, x) in enumerate(rows, 1):
        it = {"id": n, "tag": tag, "type": kind, "label": label, "y": y, "x": x}
        if role:
            it["role"] = role
        if kind == "checkbox":
            it["checked"] = False
        elif value is not None:
            it["value"] = value
        if label in ("From", "To"):
            it["placeholder"] = "Pick a date"
        items[n] = it
    return items


def test_blocks_are_found_and_each_gets_its_own_jobs_dates(master):
    W.save_answers(W.questions(), _dates_given())
    items = _smartrecruiters_form()
    found = [b for b in W.blocks(list(items.values())) if W.is_history_block(b)]
    assert len(found) == 2 and found[0]["anchor"]["value"] == "Acme Labs"
    assert [r for r, _ in found[0]["fields"]][:1] == ["title"]              # the title left of the company is its
    fills, names, inside = autofill.history_plan(list(items.values()))
    assert fills["4"] == "03/2024" and "5" not in fills and fills["6"] == "true"   # Acme: Mar 2024 -> still there
    assert fills["3"] == "Hyderabad"
    assert fills["9"] == "06/2023" and fills["10"] == "08/2025" and "11" not in fills   # Beta: Jun 2023 - Aug 2025
    assert inside >= set(items)


def test_greenhouse_style_empty_block_gets_the_latest_job(master):
    W.save_answers(W.questions(), _dates_given())
    rows = [("Company name", 10), ("Title", 60), ("Start date month", 120), ("Start date year", 120),
            ("End date month", 180), ("End date year", 180)]
    items = [{"id": n, "tag": "input", "type": "text", "label": label, "y": y, "x": n,
              **({"role": "combobox"} if "month" in label else {})} for n, (label, y) in enumerate(rows, 1)]
    items.append({"id": 7, "tag": "input", "type": "checkbox", "label": "Current role", "checked": False, "y": 180, "x": 900})
    fills, _, _ = autofill.history_plan(items)
    assert fills == {"1": "Acme Labs", "2": "Founder & Product Lead", "3": "March", "4": "2024", "7": "true"}


def test_invented_dates_are_refused_and_the_ai_is_sent_to_ask_job_dates(master, monkeypatch):
    items = _smartrecruiters_form()

    class Fake(browser.BrowserSession):
        pass
    fake = Fake()
    fake.items = items
    monkeypatch.setattr(browser, "_current", lambda: fake)
    stop = browser.answer_problem(items[4], "03/2024")                        # dates unknown yet
    assert "Karya doesn't know when the user started as Founder & Product Lead at Acme Labs" in stop
    assert "ask_job_dates" in stop
    W.save_answers(W.questions(), _dates_given())
    assert browser.answer_problem(items[4], "03/2024") is None                # the user's own date
    assert "not \"01/2023\"" in browser.answer_problem(items[4], "01/2023")   # another date for that job
    assert browser.answer_problem(items[9], "Jun 2023") is None               # Beta's block, Beta's date
    assert browser.answer_problem(items[6], "true") is None                   # Acme is current
    assert "isn't current" in browser.answer_problem(items[11], "true")        # Beta ended Aug 2025
    assert browser.answer_problem(items[11], "false") is None                 # unticking claims nothing


def test_a_lone_start_date_is_still_when_you_can_start(master, monkeypatch):
    from karya import answers
    item = {"id": 1, "tag": "input", "type": "text", "label": "Start date", "y": 10}

    class Fake(browser.BrowserSession):
        pass
    fake = Fake()
    fake.items = {1: item}
    monkeypatch.setattr(browser, "_current", lambda: fake)
    answers.save("Notice period", "Immediately")
    assert browser.answer_problem(item, "Immediately") is None               # not a job's date: the notice period
    assert W.check(item, "Immediately", [item]) == (False, None)


def test_value_formats_follow_the_field():
    d = {"month": 3, "year": 2024, "present": False}
    assert W.value_for({"type": "month"}, "full", d) == "2024-03"
    assert W.value_for({"type": "date"}, "full", d) == "2024-03-01"
    assert W.value_for({"placeholder": "MM/YY"}, "full", d) == "03/24"
    assert W.value_for({"placeholder": "DD/MM/YYYY"}, "full", d) == "01/03/2024"
    assert W.value_for({"placeholder": "Pick a date"}, "full", d) == "03/2024"
    assert W.value_for({}, "month", d) == "March" and W.value_for({}, "year", d) == "2024"
    assert W.value_for({}, "full", {"month": None, "year": 2024, "present": False}) is None   # no month: not guessed


def test_the_form_on_another_address_belongs_to_the_job_being_applied_to():
    apply_queue.start([{"id": "J1", "title": "PM", "company": "Swiggy",
                        "url": "https://jobs.smartrecruiters.com/SWIGGY/6000000001420615-product-manager-i"}])
    job = apply_queue.load()[0]
    form = "https://jobs.smartrecruiters.com/oneclick-ui/company/SWIGGY/publication/56943e0a-3cac-43db-9c91-05cdf711821c"
    assert apply_queue.job_for_page(form) is None                            # not known before it was opened
    apply_queue.set_current(job, job["url"])
    assert apply_queue.job_for_page(form)["id"] == "J1"                       # same site, recent: the same application
    assert apply_queue.job_for_page("https://www.linkedin.com/feed/") is None
    apply_queue.mark("J1", "applied")
    assert apply_queue.job_for_page(form) is None                            # done: no longer the current one


def test_calls_written_with_the_name_first_are_read():
    msg = kiro_bridge.parse_reply('<tool_call>browser_fill\n{"arguments": {"fields": {"4": "x"}}}</tool_call>')
    assert msg["tool_calls"][0]["function"]["name"] == "browser_fill"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"fields": {"4": "x"}}
    assert kiro_bridge.parse_reply("<tool_call>browser_snapshot</tool_call>")["tool_calls"][0]["function"]["arguments"] == "{}"
    broken = kiro_bridge.parse_reply("<tool_call>browser_type_secret</arg_value></tool_call>")["tool_calls"][0]
    assert "_unparsed" in broken["function"]["arguments"]                     # required arguments missing: not run


def test_the_agent_asks_job_dates_once_with_the_basics(master):
    from karya import answers
    from karya.agent import Agent
    from .conftest import FakeLLM
    for q, _, _ in answers.BASICS:
        answers.save(q, "Hyderabad" if "city" in q.lower() else "x")
    shown = []

    async def ask(request):
        shown.append(request)
        return {"answers": _dates_given()}
    agent = Agent(llm=FakeLLM([]), persist=False)
    agent.ask = ask
    asyncio.run(agent._ask_basics())
    assert len(shown) == 1 and all(not k.startswith("_") for q in shown[0]["questions"] for k in q)
    assert json.loads(resume.MASTER_FILE.read_text(encoding="utf-8"))["experience"][0]["start"] == "Mar 2024"
    out = asyncio.run(agent._ask_user("c1", {"questions": ["What month did you start at Acme Labs?"]}))
    assert out.startswith("NOT ASKED") and "ask_job_dates" in out
