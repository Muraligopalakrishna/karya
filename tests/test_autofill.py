"""One-step autofill: a job form gets everything Karya knows in one call, never a guess, never a submit.

The user, 2026-10-07: "make it faster and quick it has to same as the other apps"."""
import json

import pytest

from karya import answers, apply_queue
from karya.memory import memory_store
from karya.tools import autofill, browser, jobs, resume

JOB_URL = "https://job-boards.greenhouse.io/acme/jobs/7654321"
PROFILE = {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91 90000 00000", "location": "Hyderabad, India",
           "linkedin": "https://www.linkedin.com/in/asha-rao", "portfolio": "https://asha.example.com",
           "notice_period": "Immediately", "gender": "Female", "years_experience": "2"}


def _form():
    rows = [
        {"tag": "input", "type": "text", "label": "First Name", "name": "first_name", "required": True},
        {"tag": "input", "type": "text", "label": "Last Name", "name": "last_name", "required": True},
        {"tag": "input", "type": "email", "label": "Email", "name": "email", "required": True},
        {"tag": "input", "type": "tel", "label": "Phone", "name": "phone", "required": True},
        {"tag": "input", "type": "text", "label": "Location (City)", "role": "combobox", "required": True},
        {"tag": "input", "type": "file", "label": "Resume/CV", "required": True},
        {"tag": "input", "type": "text", "label": "LinkedIn Profile", "required": False},
        {"tag": "input", "type": "text", "label": "Website", "required": False},
        {"tag": "input", "type": "text", "label": "Reference name", "required": False},
        {"tag": "input", "type": "text", "label": "Phone country code", "required": False},
        {"tag": "textarea", "label": "What is your notice period?", "required": True},
        {"tag": "select", "label": "Gender", "options": ["Select...", "Male", "Female", "Decline to self-identify"],
         "value": "Select...", "required": False},
        {"tag": "input", "type": "radio", "label": "Yes", "question": "Are you legally authorized to work in India?",
         "checked": False, "required": True},
        {"tag": "input", "type": "radio", "label": "No", "question": "Are you legally authorized to work in India?",
         "checked": False, "required": True},
        {"tag": "textarea", "label": "Why do you want to work at Acme?", "required": True},
        {"tag": "input", "type": "text", "label": "Expected CTC", "required": True},
        {"tag": "input", "type": "text", "label": "Start date year", "required": True},
        {"tag": "button", "label": "Submit application"},
    ]
    return {i: {"id": i, **row} for i, row in enumerate(rows, 1)}


class FakeForm(browser.BrowserSession):
    """A Greenhouse-like form. Records what Karya puts where; nothing is ever submitted."""

    def __init__(self, items):
        super().__init__()
        self.url, self.form = JOB_URL, items
        self.filled, self.uploaded, self.clicked = {}, {}, []

    def call(self, fn, *a):
        return fn(*a)

    def _ensure(self):
        raise AssertionError("tests never start a real browser")

    def options_of(self, element_id):
        return list(self.form[int(element_id)].get("menu") or [])

    def snapshot(self, filter_text=None, max_items=100):
        self.items = {i: dict(it) for i, it in self.form.items()}
        return "URL: " + self.url + "\n" + "\n".join(browser._fmt(it) for it in self.items.values())

    def fill_many(self, fields):
        for raw_id, value in fields.items():
            it = self.form[int(raw_id)]
            if it.get("type") == "radio":
                it["checked"] = True
            else:
                it["value"] = str(value)
            self.filled[it.get("label")] = str(value)
        return f"Filled {len(fields)} field(s)\nURL: {self.url}"

    def upload(self, element_id, file_path):
        self.form[int(element_id)]["value"] = file_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        self.uploaded[int(element_id)] = file_path
        return f"Uploaded {file_path}"

    def click(self, element_id=None, text=None, double=False):
        self.clicked.append(element_id)
        return "Clicked"

    def form_check(self, element_id):
        return {"empty": [it["label"] for it in self.form.values() if it.get("required") and not it.get("value")
                          and it.get("type") != "radio"]}

    def all_text(self):
        return "Acme - Product Manager. Apply for this job."


@pytest.fixture()
def page(monkeypatch, tmp_path):
    data = memory_store.load()
    data.setdefault("profile", {}).update(PROFILE)
    memory_store.save(data)
    monkeypatch.setattr(jobs, "read_resume", lambda path=None: "Asha Rao. Product analyst at Beta (2023 - Present).")
    pdf = tmp_path / "Asha_Rao_Resume_Acme.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    resume.remember_tailored(pdf, "Acme", "Product Manager")
    apply_queue.start([{"id": "J1", "title": "Product Manager", "company": "Acme", "url": JOB_URL}])
    fake = FakeForm(_form())
    monkeypatch.setattr(browser, "session", fake)
    monkeypatch.setattr(browser, "_current", lambda: fake)
    monkeypatch.setattr(browser, "_use", lambda: fake)
    monkeypatch.setattr(answers, "RECENT_USER", ["apply to the Acme job"])
    return fake, pdf


def test_one_step_fills_everything_known(page):
    fake, pdf = page
    out = autofill.apply_autofill()
    f = fake.filled
    assert f["First Name"] == "Asha" and f["Last Name"] == "Rao" and f["Email"] == "asha@example.com"
    assert f["Phone"] == "+91 90000 00000" and f["Location (City)"] == "Hyderabad, India"
    assert f["LinkedIn Profile"].endswith("/asha-rao") and f["Website"] == "https://asha.example.com"
    assert f["What is your notice period?"] == "Immediately"                   # the user's saved answer
    assert f["Gender"] == "Female"                                              # the matching option
    assert fake.uploaded == {6: str(pdf)}                                       # the resume made for THIS job
    assert out.startswith("AUTOFILL: filled 10 field(s) in one step")
    assert fake.clicked == []                                                   # never submits


def test_never_guesses_and_lists_what_is_left(page):
    fake, _ = page
    out = autofill.apply_autofill()
    for label in ("Reference name", "Phone country code", "Why do you want to work at Acme?", "Expected CTC",
                  "Start date year"):
        assert label not in fake.filled, label              # someone else's detail, no answer yet, or a guess
    assert "Are you legally authorized to work in India?" not in " ".join(fake.filled)   # a rule, not a fact
    left = out.split("Still open")[1]
    assert "Why do you want to work at Acme?" in left and "answer it from the resume and the job" in left
    assert "Expected CTC" in left and "ask_user" in left
    assert "Are you legally authorized to work in India?" in left and "First Name" not in left
    assert "ONE browser_fill" in out


def test_saved_answers_fill_choice_questions_and_selects(page):
    fake, _ = page
    answers.save("Are you legally authorized to work in India?", "Yes, I'm an Indian citizen")
    answers.save("Expected CTC", "12 LPA")
    autofill.apply_autofill()
    assert fake.form[13]["checked"] is True and not fake.form[14].get("checked")     # "Yes", not "No"
    assert fake.filled["Expected CTC"] == "12 LPA"
    assert autofill.pick_option(["Select...", "Yes", "No"], "yes, I'm 24") == "Yes"
    assert autofill.pick_option(["Select...", "0-1 years", "1-3 years"], "5 years") is None   # nothing matches: ask
    assert autofill.pick_option(["Male", "Female"], "") is None


def test_filled_fields_are_left_alone(page):
    fake, _ = page
    fake.form[1]["value"] = "Asha R."              # the user typed it themselves
    autofill.apply_autofill()
    assert "First Name" not in fake.filled and fake.form[1]["value"] == "Asha R."


def test_runs_by_itself_when_a_picked_job_opens(page, monkeypatch):
    fake, pdf = page
    monkeypatch.setattr(fake, "open", lambda url, new_tab=False: fake.snapshot(), raising=False)
    out = browser.browser_open(JOB_URL)
    assert "AUTOFILL: filled" in out and fake.uploaded == {6: str(pdf)}
    fake.filled.clear()
    monkeypatch.setattr(fake, "url", "https://job-boards.greenhouse.io/other/jobs/1", raising=False)
    assert "AUTOFILL" not in browser.browser_open("https://job-boards.greenhouse.io/other/jobs/1")   # not picked
    assert fake.filled == {}


def test_no_tailored_resume_says_so(page, tmp_path):
    fake, pdf = page
    pdf.unlink()
    out = autofill.apply_autofill()
    assert fake.uploaded == {} and "no resume tailored for this job yet" in out and "tailor_resume" in out


def test_a_dropdown_that_did_not_take_is_reported(page, monkeypatch):
    # 2026-10-07, Razorpay: the report said "Gender" was filled while the page still showed "Select..."
    fake, _ = page
    fake.form[12] = {"id": 12, "tag": "input", "type": "text", "role": "combobox", "label": "Gender*", "required": True}
    real_fill = fake.fill_many

    def fill(fields):
        out = real_fill(fields)
        fake.form[12].pop("value", None)          # the page's list had no matching option
        return out
    monkeypatch.setattr(fake, "fill_many", fill)
    out = autofill.apply_autofill()
    assert "Gender" not in out.split("Didn't take")[0].split("in one step:")[1]
    assert "Didn't take (no matching option, or the page rejected it): Gender" in out


def test_open_dropdowns_list_their_options(page):
    fake, _ = page
    fake.form[20] = {"id": 20, "tag": "input", "type": "text", "role": "combobox", "label": "Current Career Stage*",
                     "required": True, "menu": ["Experienced Professional", "College Grads / Fresher"]}
    industries = [f"Industry {n}" for n in range(1, 28)]
    fake.form[21] = {"id": 21, "tag": "input", "type": "text", "role": "combobox", "label": "Current Industry*",
                     "required": True, "menu": industries}
    out = autofill.apply_autofill()
    line = next(x for x in out.splitlines() if "Current Career Stage" in x)
    assert "options=Experienced Professional|College Grads / Fresher" in line
    line = next(x for x in out.splitlines() if "Current Industry" in x)
    assert "Industry 1|" in line and "more options: Industry 13|" in line and "Industry 27" in line


def test_company_and_title_come_from_the_latest_job(page, monkeypatch):
    fake, _ = page
    monkeypatch.setattr(resume, "load_master", lambda: {"experience": [
        {"title": "Founder & Product Lead", "company": "Acme Labs", "start": "", "end": ""},
        {"title": "Intern", "company": "Beta", "start": "Jul 2024", "end": "Sep 2024"}]})
    fake.form[30] = {"id": 30, "tag": "input", "type": "text", "label": "Company name", "required": True}
    fake.form[31] = {"id": 31, "tag": "input", "type": "text", "label": "Title", "required": True}
    fake.form[32] = {"id": 32, "tag": "input", "type": "text", "label": "Company name", "required": False}
    fake.form[33] = {"id": 33, "tag": "input", "type": "text", "role": "combobox", "label": "Current Designation*"}
    fake.form[34] = {"id": 34, "tag": "input", "type": "text", "label": "Current Company", "required": True}
    autofill.apply_autofill()
    assert fake.form[30]["value"] == "Acme Labs" and fake.form[31]["value"] == "Founder & Product Lead"
    assert fake.form[32].get("value") == "Beta"                                  # a second block: the next job
    assert not fake.form[33].get("value")                                        # a list: the AI picks from it
    assert fake.form[34]["value"] == "Acme Labs"                                 # "current" is always the latest


def test_greenhouse_attach_is_the_resume_upload(page):
    fake, pdf = page
    fake.form[6] = {"id": 6, "tag": "input", "type": "file", "label": "Attach", "key": "resume", "required": True}
    fake.form[19] = {"id": 19, "tag": "input", "type": "file", "label": "Attach", "key": "cover_letter"}
    autofill.apply_autofill()
    assert fake.uploaded == {6: str(pdf)}                                       # not the cover letter


def test_passwords_are_never_saved_asked_or_shown(monkeypatch):
    # a sign-up form's "Password (create a strong one)" was asked with ask_user and saved in plain text
    from karya import secrets_filter, vault
    answers.save("Password (create a strong one)", "Sup3r-Secret-99")
    answers.save("Notice period", "Immediately")
    assert "password create a strong one" not in json.dumps(memory_store.load())
    assert answers.saved_answer("Password") is None and answers.saved_answers().get("notice period") == "Immediately"
    vault.save_account("example.com", "asha", "Vault-Pass-123")
    assert "Vault-Pass-123" not in secrets_filter.scrub("the page says Vault-Pass-123 here")
    assert "password" not in json.dumps(jobs.get_application_profile()).lower().replace("passwords", "")
    assert answers.classify("Expected Comp (Please mention fixed comp below which you are not willing to consider an "
                            "offer)*") == "expected_salary"
    assert answers.classify("Current Compensation (Fixed + Variable)") == "current_salary"


def test_ask_user_refuses_password_questions():
    import asyncio
    from karya.agent import Agent
    from .conftest import FakeLLM
    agent = Agent(llm=FakeLLM([]), persist=False)
    agent.ask = lambda request: (_ for _ in ()).throw(AssertionError("the card must not be shown"))
    out = asyncio.run(agent._ask_user("c1", {"questions": ["Password (create a strong one)", "Notice period"]}))
    assert out.startswith("NOT ASKED") and "request_credentials" in out


# ---------------------------------------------------------------- Lever / Paytm (2026-10-08)
def test_mixed_up_values_are_refused_and_autofill_fixes_them(page):
    fake, _ = page
    fake.form[1]["value"] = "Noida, Uttar Pradesh"          # the AI put the location into "First Name"
    fake.form[7]["value"] = "Asha Rao"                      # ...and the name into "LinkedIn Profile"
    assert "isn't the user's name" in browser.shape_problem({"tag": "input", "label": "Full name ✱"}, "Noida, Uttar Pradesh")
    assert "isn't a LinkedIn link" in browser.answer_problem(fake.form[7], "Asha Rao")
    assert browser.answer_problem(fake.form[7], "https://www.linkedin.com/in/asha-rao") is None
    autofill.apply_autofill()
    assert fake.filled["First Name"] == "Asha" and fake.filled["LinkedIn Profile"].endswith("/asha-rao")


def test_a_country_list_gets_the_users_country(page):
    fake, _ = page
    fake.form[12] = {"id": 12, "tag": "select", "label": "What is your location?", "value": "Select...",
                     "options": ["Select...", "Afghanistan", "Albania"] + ["Country %d" % n for n in range(60)] + ["India"]}
    autofill.apply_autofill()
    assert fake.filled["What is your location?"] == "India"
    line = browser._fmt(fake.form[12])
    assert "(+52 more; browser_select with the option's text, e.g. India)" in line


def test_new_questions_after_a_choice_are_autofilled(page, monkeypatch):
    fake, _ = page
    calls = []
    monkeypatch.setattr(autofill, "run", lambda resume_path="": calls.append(1) or "AUTOFILL: filled 2 field(s) in one step: x.")
    before = set(fake.items or fake.form)
    fake.items = {**{i: dict(it) for i, it in fake.form.items()},
                  99: {"id": 99, "tag": "input", "type": "text", "label": "PAN number"}}
    out = browser._more_fields(before)
    assert calls and out.startswith("\n\nNEW FIELDS APPEARED. AUTOFILL: filled 2")
    assert browser._more_fields(set(fake.items)) == ""                        # nothing new: nothing runs


def test_phone_without_the_country_code_when_the_form_has_its_own_code_field(page):
    fake, _ = page
    fake.form[4]["label"] = "Phone Number*"
    fake.form[20] = {"id": 20, "tag": "input", "type": "text", "role": "combobox", "label": "Country Phone Code*",
                     "value": "India (+91)"}
    autofill.apply_autofill()
    assert fake.filled["Phone Number*"] == "90000 00000"                      # Workday wants the national number
    assert "Country Phone Code*" not in fake.filled
