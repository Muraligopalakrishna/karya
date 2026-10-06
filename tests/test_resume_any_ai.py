"""Resumes when Karya has no AI of its own (someone using it only from another AI app over MCP): the app's AI writes the
resume JSON, and Karya checks every fact against the user's real resume before it makes the PDF."""
import json

import pytest

from karya import llm
from karya.registry import run_tool
from karya.tools import jobs, resume

MASTER = {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91 90000 00000", "location": "Hyderabad",
          "summary": "Product builder who shipped 2 products.", "skills": ["SQL", "Figma", "Product Management"],
          "experience": [{"title": "Founder", "company": "Acme Labs", "start": "2023", "end": "Present",
                          "bullets": ["Built 2 products used by 300 people", "Ran user interviews"]}],
          "projects": [{"name": "Trade Replay", "description": "Practice trading on past charts",
                        "bullets": ["Shipped in 3 weeks"]}],
          "education": [{"degree": "B.Tech CS", "school": "GITAM University", "start": "2020", "end": "2024"}]}
JOB = {"job_title": "Associate Product Manager", "company": "Zeta", "job_description": "APM: SQL, Kubernetes, user research"}


@pytest.fixture()
def built(monkeypatch):
    """No AI in Karya, and the PDF step is recorded instead of run."""
    monkeypatch.setattr(llm, "ACTIVE", None)
    made = []
    monkeypatch.setattr(resume, "build_resume", lambda r, job_title="", company="": made.append(r) or {"pdf": "x.pdf"})
    return made


def test_tailor_without_own_ai_asks_the_app_then_checks_every_fact(built):
    resume.save_resume_data(MASTER)
    ask = json.loads(run_tool("tailor_resume", JOB))
    assert ask["karya_needs"] and ask["master_resume"]["name"] == "Asha Rao" and "Never add" in ask["rules"]
    assert built == []                                                        # nothing made yet
    written = dict(MASTER, summary="APM with 10 years of experience", email="someone@else.com",
                   skills=["Product Management", "SQL", "Kubernetes"], suggested_skills=["Jira"],
                   experience=[{"title": "CEO", "company": "Acme Labs", "start": "2019", "end": "Present",
                                "bullets": ["Grew revenue 40%", "Built 2 products used by 300 people"]},
                               {"title": "PM", "company": "Google", "bullets": ["Led Search"]}])
    out = json.loads(run_tool("tailor_resume", dict(JOB, resume=written)))
    made = built[0]
    assert [e["company"] for e in made["experience"]] == ["Acme Labs"]                 # the invented job is gone
    assert (made["experience"][0]["title"], made["experience"][0]["start"]) == ("Founder", "2023")   # real title/dates
    assert made["experience"][0]["bullets"] == ["Built 2 products used by 300 people"]  # the made-up 40% is dropped
    assert "Kubernetes" not in made["skills"] and {"Kubernetes", "Jira"} <= set(out["suggested_skills"])
    assert made["summary"] == MASTER["summary"] and made["email"] == "asha@example.com"
    assert made["projects"] == MASTER["projects"]                                   # a section left out comes back
    assert any("Google" in note for note in out["checked"])


def test_import_resume_without_own_ai_keeps_only_whats_in_the_file(built, monkeypatch):
    text = ("ASHA RAO | asha@example.com | +91 90000 00000\nFounder, Acme Labs (2023 - Present)\n- Built 2 products used "
            "by 300 people\nSkills: SQL, Figma, Product Management\nEducation: B.Tech CS, GITAM (Deemed to be University)")
    monkeypatch.setattr(jobs, "read_resume", lambda path=None: text)
    ask = json.loads(run_tool("import_resume", {}))
    assert ask["karya_needs"] and "Acme Labs" in ask["resume_text"] and resume.load_master() is None
    converted = {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91 90000 00000",
                 "skills": ["SQL", "Figma", "Kubernetes"],
                 "experience": [{"title": "Founder", "company": "Acme Labs",
                                 "bullets": ["Built 2 products used by 300 people", "Grew revenue 40%"]},
                                {"title": "Engineer", "company": "Google", "bullets": []}],
                 "education": [{"degree": "B.Tech CS", "school": "GITAM University"}]}
    out = json.loads(run_tool("import_resume", {"resume": converted}))
    saved = resume.load_master()
    assert saved["skills"] == ["SQL", "Figma"] and [e["company"] for e in saved["experience"]] == ["Acme Labs"]
    assert saved["experience"][0]["bullets"] == ["Built 2 products used by 300 people"]
    assert saved["education"][0]["school"] == "GITAM University" and saved["name"] == "Asha Rao"
    assert any("Kubernetes" in note for note in out["checked"]) and any("Google" in note for note in out["checked"])


def test_own_ai_that_fails_hands_the_work_to_the_app(built, monkeypatch):
    class NotRunning:                     # e.g. only the default Ollama entry, and Ollama isn't installed
        providers = ["ollama"]

        def complete(self, system, user):
            raise ConnectionError("connection refused")
    monkeypatch.setattr(llm, "ACTIVE", NotRunning())
    resume.save_resume_data(MASTER)
    assert json.loads(run_tool("tailor_resume", JOB))["karya_needs"]


def test_own_ai_is_used_when_it_works(built, monkeypatch):
    class Works:
        providers = ["groq"]

        def complete(self, system, user):
            return json.dumps(dict(MASTER, summary="Builder for fintech products."))
    monkeypatch.setattr(llm, "ACTIVE", Works())
    resume.save_resume_data(MASTER)
    out = json.loads(run_tool("tailor_resume", JOB))
    assert "karya_needs" not in out and built[0]["summary"] == "Builder for fintech products."
