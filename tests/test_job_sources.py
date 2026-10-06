"""Job sources beyond LinkedIn, in any country: Workday, SmartRecruiters, big tech, Indian/US/EU boards, search
discovery, learned company boards, and recently funded companies. Every HTTP call is faked."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from karya.registry import run_tool
from karya.tools import funding as F
from karya.tools import job_sources as S
from karya.tools import jobs as J


def _no_network(*a, **k):
    raise requests.ConnectionError("tests never use the internet")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for mod, name in ((S, "_get"), (S, "_get_json"), (S, "_get_text"), (S, "_post_json"), (J, "_get_json")):
        monkeypatch.setattr(mod, name, _no_network)
    monkeypatch.setattr(S, "_search", lambda *a, **k: [])
    monkeypatch.setattr(J, "user_skills", lambda: set())


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------ places
def test_location_fit_any_country_whole_words():
    fit = lambda loc, places: J._location_fit({"location": loc}, places, True)  # noqa: E731
    assert fit("Berlin, DE", ["Germany"])[2] and fit("Singapore", ["Singapore"])[2]
    assert fit("Amsterdam, Netherlands", ["Europe"])[2] and fit("Bengaluru, Karnataka", ["Hyderabad"])[2]
    assert not fit("Kyiv, Ukraine", ["UK"])[2] and not fit("Milwaukee, WI", ["UK"])[2]      # no more substring hits
    pts, why, ok = fit("Remote: Congo, The Democratic Republic", ["India", "Remote"])
    assert ok and pts < 0 and "limited" in why                                              # shown, ranked low
    assert fit("Remote - India", ["India"])[0] == 15


def test_places_and_roles():
    assert S.place_key("Gurgaon") == "india" and S.place_key("USA") == "united states" and S.place_key("x") is None
    assert S.is_city("London") and not S.is_city("United Kingdom")
    assert S.api_places(["Remote", "India", "Pune", "UK"]) == [("India", "india"), ("Pune", "india")]
    assert S.roles_of("associate product manager, product manager, product analyst") == ["product manager", "product analyst"]


def test_city_ranks_above_rest_of_country_and_regional_remote():
    fit = lambda loc, places: J._location_fit({"location": loc}, places, True)  # noqa: E731
    assert fit("Hyderabad, Telangana", ["Hyderabad"])[:2] == (15, "in Hyderabad")
    pts, why, ok = fit("Bengaluru, India", ["Hyderabad"])
    assert ok and pts == 9 and why == "in India, not Hyderabad"
    assert fit("Remote: APAC", ["India", "Remote"])[0] == 13                                 # APAC includes India
    assert fit("Remote (EMEA only)", ["India", "Remote"])[0] < 0
    assert fit("Remote - India", ["Hyderabad"])[:2] == (13, "remote (India)")
    assert fit("Remote (United States)", ["India", "Remote"])[1] == "remote but limited to United States"


def test_company_boards_reads_only_new_boards(monkeypatch):
    fetched = []

    def board(slug, ats, name=None):
        fetched.append(slug)
        return [{"title": "Product Manager", "location": "Remote", "url": f"https://x/{slug}", "company": slug,
                 "source": f"{slug} careers ({ats.title()})"}]
    monkeypatch.setattr(J, "_board", board)
    J.src_companies("product manager", [])
    first = len(fetched)
    assert first == len(J.COMPANY_BOARDS)
    S.learn({"ats": "ashby", "slug": "newco", "key": "ashby:newco"}, company="NewCo")
    rows = J.src_companies("product manager", [])
    assert fetched[first:] == ["newco"] and any(r["url"] == "https://x/newco" for r in rows)


# ------------------------------------------------------------------ Workday
FACETS = [{"facetParameter": "Location_Country", "values": [{"descriptor": "India", "id": "IND", "count": 21},
                                                           {"descriptor": "United States of America", "id": "USA", "count": 300}]},
          {"facetParameter": "Location_Region_State_Province", "values": [{"descriptor": "Indiana", "id": "IN-STATE", "count": 1}]},
          {"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
              {"descriptor": "IND.Pune", "id": "PUNE", "count": 3}, {"descriptor": "USA, CA, San Jose", "id": "SJ", "count": 9}]}]},
          {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Product Management", "id": "PM", "count": 9},
                                                          {"descriptor": "Sales", "id": "SALES", "count": 40}]}]


def _workday(monkeypatch, filtered):
    calls = []

    def post(url, body, timeout=15):
        calls.append((url, json.loads(json.dumps(body))))
        if not body["appliedFacets"]:
            return {"total": 400, "facets": FACETS, "jobPostings": [
                {"title": "Sales Rep", "externalPath": "/job/Austin/Sales-Rep_1", "locationsText": "Austin", "postedOn": "Posted Today"}]}
        return {"total": len(filtered), "jobPostings": filtered}
    monkeypatch.setattr(S, "_post_json", post)
    return calls


def test_workday_filters_by_country_and_job_family(monkeypatch):
    calls = _workday(monkeypatch, [
        {"title": "Associate Product Manager", "externalPath": "/job/Pune/Associate-Product-Manager_JR1",
         "locationsText": "IND.Pune", "postedOn": "Posted 3 Days Ago"},
        {"title": "Product Manager", "externalPath": "/job/Multi/Product-Manager_JR2", "locationsText": "2 Locations",
         "postedOn": "Posted 30+ Days Ago"}])
    rows = S._wd_search("Workday", "workday", "wd5", "Workday", "product", "product manager", ["India"])
    assert calls[0][0] == "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs"
    # one place parameter only ("Indiana" isn't India), plus the matching job family
    assert calls[1][1]["appliedFacets"] == {"Location_Country": ["IND"], "jobFamilyGroup": ["PM"]}
    assert rows[0]["url"] == "https://workday.wd5.myworkdayjobs.com/Workday/job/Pune/Associate-Product-Manager_JR1"
    assert rows[0]["posted"] == _days_ago(3) and rows[0]["ats"] == "workday" and rows[0]["direct"]
    assert rows[1]["location"] == "2 Locations incl. India" and rows[1]["posted"] == _days_ago(31)
    assert J.apply_via(rows[0]).startswith("company site on Workday (needs a free account")


def test_workday_city_filter_and_skip_when_no_jobs_there(monkeypatch):
    calls = _workday(monkeypatch, [])
    S._wd_search("Workday", "workday", "wd5", "Workday", "product", "product manager", ["Pune"])
    assert calls[1][1]["appliedFacets"]["locations"] == ["PUNE"]
    calls.clear()
    assert S._wd_search("Workday", "workday", "wd5", "Workday", "product", "product manager", ["Germany"]) == []
    assert len(calls) == 1                                    # no Germany facet: nothing there, no second request


def test_workday_tenants_include_learned_and_respect_company_type():
    S.learn({"ats": "workday", "tenant": "pg", "wd": "wd5", "site": "1000", "key": "workday:pg|wd5|1000"}, company="P&G")
    tenants = S._workday_tenants()
    assert ("P&G", "pg", "wd5", "1000", None) in tenants
    assert all(t[4] == "service" for t in S._workday_tenants(["service"]))


# ------------------------------------------------------------------ SmartRecruiters + big tech
def test_smartrecruiters_rows(monkeypatch):
    def get_json(url, params=None, timeout=15):
        assert url.endswith("/companies/PHONEPELIMITED/postings") and params["country"] == "in"
        return {"content": [{"id": "744000153211799", "name": "Product Manager", "company": {"name": "PHONEPE LIMITED"},
                             "releasedDate": "2026-10-02T15:03:01.251Z",
                             "location": {"city": "Bengaluru", "country": "in", "fullLocation": "Bengaluru, , India"}}]}
    monkeypatch.setattr(S, "_get_json", get_json)
    row = S._sr_search("PhonePe", "PHONEPELIMITED", "product", "product manager", "in")[0]
    assert row["url"] == "https://jobs.smartrecruiters.com/PHONEPELIMITED/744000153211799"
    assert row["company"] == "PhonePe" and row["location"] == "Bengaluru, India"
    assert J.apply_via(row) == "company form, no login (SmartRecruiters)"


def test_big_tech_parsers(monkeypatch):
    google_job = ["123", "Product Manager, Google Photos", "https://signin", [None, "<ul><li>Own the roadmap</li></ul>"],
                  [None, "<ul><li>5 years of experience in product management.</li></ul>"], "p", None, "Google", "en-US",
                  [["Bengaluru, Karnataka, India", ["addr"], "Bengaluru", "560038", "KA", "IN"]], [None, ""], [2], [1790865661, 0]]
    html = ('<a href="jobs/results/123-product-manager-google-photos?q=pm">x</a><script>AF_initDataCallback({key: '
            "'ds:1', hash: '2', data:" + json.dumps([[google_job], None, 1]) + ", sideChannel: {}});</script>")

    def get_json(url, params=None, timeout=15):
        if "amazon.jobs" in url:
            assert params["country"] == "IND"
            return {"jobs": [{"title": "Product Manager ", "location": "IN, KA, Bengaluru", "posted_date": "October  5, 2026",
                              "job_path": "/en/jobs/10568621/pm", "basic_qualifications": "- 3+ years of PM experience"}]}
        if "microsoft" in url:
            return {"data": {"positions": [{"id": 1970393556982420, "name": "Product Manager",
                                            "locations": ["India, Telangana, Hyderabad"], "postedTs": 1790863146}]}}
        if "netflix" in url:
            return {"positions": [{"id": 790317836990, "name": "Product Manager", "location": "Mumbai,India",
                                   "t_create": 1790294400, "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/790317836990"}]}
        if "atlassian" in url:
            return [{"id": 25480, "title": "Associate Product Manager", "locations": ["Bengaluru - India -   Bengaluru"],
                     "overview": "<p>Hi</p>", "responsibilities": "", "qualifications": "", "applyUrl": "https://icims/apply",
                     "portalJobPost": {"updatedDate": "2026-09-24 03:33 PM"}}]
        raise AssertionError(url)
    monkeypatch.setattr(S, "_get_json", get_json)
    monkeypatch.setattr(S, "_get_text", lambda url, params=None, timeout=20: html)
    places = S.api_places(["India"])
    amazon = S.src_amazon("product manager", places)[0]
    assert amazon["posted"] == "2026-10-05" and amazon["url"] == "https://www.amazon.jobs/en/jobs/10568621/pm"
    assert "3+ years" in amazon["text"] and amazon["title"] == "Product Manager"
    ms = S.src_microsoft("product manager", places)[0]
    assert ms["url"] == "https://apply.careers.microsoft.com/careers/job/1970393556982420"
    google = S.src_google("product manager", places, "entry")[0]
    assert google["url"].endswith("/jobs/results/123-product-manager-google-photos")
    assert google["location"].startswith("Bengaluru") and "5 years" in google["text"]
    assert S.src_netflix("product manager", places)[0]["company"] == "Netflix"
    atl = S.src_atlassian("product manager", places)[0]
    assert atl["url"] == "https://www.atlassian.com/company/careers/details/25480" and atl["posted"] == "2026-09-24"


# ------------------------------------------------------------------ India / US / EU boards
def test_india_boards(monkeypatch):
    next_data = {"props": {"pageProps": {"dehydratedState": {"queries": [{"queryKey": ["jobListData", "x"], "state": {"data": {
        "data": {"pageData": {"jobs": [
            {"headline": "Product Manager", "publicUrl": "https://cutshort.io/job/pm-1", "locationsText": "Bengaluru",
             "companyDetails": {"name": "Gravity Engineering Services", "type": "Services"}, "expRange": {"min": 0, "max": 2}},
            {"headline": "Product Manager", "publicUrl": "https://cutshort.io/job/pm-2", "locationsText": "Pune",
             "companyDetails": {"name": "Acme"}, "hiringForClient": True}]}}}}}]}}}}

    def get_json(url, params=None, timeout=15):
        if "instahyre" in url:
            return {"objects": [{"title": "Product Manager", "locations": "Delhi", "public_url": "https://instahyre/j/1",
                                 "employer": {"company_name": "Aftershoot", "employee_count": 10}, "keywords": ["Product Management"]}]}
        if "foundit" in url:
            return {"data": [{"title": "Associate Product Manager", "companyName": "Virohan", "locations": [{"city": "Gurugram"}],
                              "postedAt": 1791184596000, "jdUrl": "/job/apm-virohan-69340679",
                              "applyUrl": "https://www.linkedin.com/jobs/view/4473717190/",
                              "minimumExperience": {"years": 5}, "maximumExperience": {"years": 10}}]}
        raise AssertionError(url)
    monkeypatch.setattr(S, "_get_json", get_json)
    monkeypatch.setattr(S, "_get_text", lambda url, params=None, timeout=20:
                        '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(next_data) + "</script>")
    rows = {r["url"]: r for r in S.src_india("product manager", ["India"], "entry")}
    assert rows["https://instahyre/j/1"]["location"] == "Delhi, India" and rows["https://instahyre/j/1"]["ctype"] == "startup"
    assert rows["https://cutshort.io/job/pm-1"]["ctype"] == "service" and rows["https://cutshort.io/job/pm-1"]["experience"] == "0-2 years"
    assert rows["https://cutshort.io/job/pm-2"]["ctype"] == "staffing"
    found = rows["https://www.foundit.in/job/apm-virohan-69340679"]
    assert found["via_linkedin"] and J.apply_via(found).startswith("LinkedIn") and J.required_years(found["text"]) == 5
    only_product = S.src_india("product manager", ["India"], "entry", ["product"])
    assert {r["company"] for r in only_product} == {"Aftershoot", "Virohan"}


def test_boards_only_run_where_they_help(monkeypatch):
    asked = []
    monkeypatch.setattr(S, "_get_json", lambda url, params=None, timeout=15: asked.append(url) or {})
    assert S.src_india("pm", ["United States"]) == [] and S.src_boards("pm", ["India"]) == [] and asked == []
    S.src_boards("pm", ["Germany"])
    assert any("themuse" in u for u in asked) and any("arbeitnow" in u for u in asked)


# ------------------------------------------------------------------ discovery + learned boards + company lookup
def test_parse_ats_urls():
    wd = S.parse_ats_url("https://pg.wd5.myworkdayjobs.com/en-US/1000/job/Mumbai/APM_R1?x=1")
    assert (wd["tenant"], wd["wd"], wd["site"], wd["job"]) == ("pg", "wd5", "1000", "/job/Mumbai/APM_R1")
    assert S.parse_ats_url("https://job-boards.greenhouse.io/razorpay/jobs/123")["key"] == "greenhouse:razorpay"
    assert S.parse_ats_url("https://jobs.lever.co/zeta/0f8a5b8e-1111-2222-3333-444455556666")["job"]
    assert S.parse_ats_url("https://jobs.smartrecruiters.com/ServiceNow/744000153614183-sr-x")["key"] == "smartrecruiters:ServiceNow"
    assert S.parse_ats_url("https://apply.workable.com/acme/j/ABC123/")["ats"] == "workable"
    assert S.parse_ats_url("https://boards.greenhouse.io/embed/job_app") is None and S.parse_ats_url("https://acme.com") is None
    assert S._split_title("Job Application for Product Manager at Razorpay", "Razorpay") == ("Product Manager", "Razorpay")
    assert S._split_title("Mactores - Associate Product Manager", "Mactores") == ("Associate Product Manager", "Mactores")


def test_discovery_reads_new_boards_and_remembers_them(monkeypatch):
    results = {"myworkdayjobs": [{"title": "Associate Product Manager",
                                  "href": "https://pg.wd5.myworkdayjobs.com/en-US/1000/job/Mumbai/Associate-Product-Manager_R1"}],
               "lever": [{"title": "Mactores - Associate Product Manager",
                          "href": "https://jobs.lever.co/mactores/0f8a5b8e-1111-2222-3333-444455556666"}],
               "workable": [{"title": "Product Manager - Acme", "href": "https://apply.workable.com/acme/j/ABC123/"}]}
    monkeypatch.setattr(S, "_search", lambda q, *a, **k: next((v for k2, v in results.items() if k2 in q), []))
    monkeypatch.setattr(S, "_post_json", lambda url, body, timeout=15: {"total": 1, "facets": [], "jobPostings": [
        {"title": "Associate Product Manager", "externalPath": "/job/Mumbai/Associate-Product-Manager_R1",
         "locationsText": "Mumbai, India", "postedOn": "Posted Yesterday"}]})
    monkeypatch.setattr(J, "_get_json", lambda url, params=None, timeout=20: [
        {"text": "Associate Product Manager", "categories": {"location": "Pune, India"}, "createdAt": 1791184596000,
         "hostedUrl": "https://jobs.lever.co/mactores/0f8a5b8e-1111-2222-3333-444455556666"}] if "lever.co" in url else _no_network())
    rows = S.src_discover("associate product manager", ["India"])
    by_company = {r["company"]: r for r in rows}
    assert by_company["Pg"]["location"] == "Mumbai, India" and by_company["Pg"]["ats"] == "workday"
    assert by_company["Mactores"]["source"] == "mactores careers (Lever)"
    assert by_company["Acme"]["source"] == "Acme careers (Workable, found by search)" and by_company["Acme"]["title"] == "Product Manager"
    assert {"workday:pg|wd5|1000", "lever:mactores"} <= set(S.learned())     # searched by name next time
    assert "workday:acme" not in S.learned()
    S.clear_cache()
    asked = []
    monkeypatch.setattr(S, "_post_json", lambda *a, **k: asked.append(a) or {"total": 0, "jobPostings": []})
    again = S.src_discover("associate product manager", ["India"])
    assert asked == [] and {r["company"] for r in again} == {"Acme"}          # known boards: not fetched or listed twice


def test_resolve_company_checks_whose_board_it_is(monkeypatch):
    def get_json(url, params=None, timeout=15):
        if url.endswith("/boards/tcs"):
            return {"name": "Thornbury Community Services"}               # a different "TCS"
        raise requests.HTTPError("404")
    monkeypatch.setattr(S, "_get_json", get_json)
    assert S.resolve_company("TCS") is None
    monkeypatch.setattr(S, "_search", lambda *a, **k: [
        {"title": "Careers", "href": "https://job-boards.greenhouse.io/acmerobotics/jobs/77"}])
    monkeypatch.setattr(J, "_get_json", lambda url, params=None, timeout=20: {"jobs": [
        {"id": 77, "title": "Product Manager", "location": {"name": "Bengaluru"},
         "absolute_url": "https://job-boards.greenhouse.io/acmerobotics/jobs/77"}]} if "/boards/acmerobotics/" in url else _no_network())
    got = S.resolve_company("Acme Robotics")
    assert got["ats"] == "greenhouse" and got["slug"] == "acmerobotics" and got["open_roles"] == 1
    assert S.learned()["greenhouse:acmerobotics"]["company"] == "Acme Robotics"
    assert J.src_companies("product manager", [])[0]["company"] == "Acme Robotics"    # now searched every time
    out = json.loads(run_tool("set_job_preferences", {"companies": ["Acme Robotics"]}))
    assert out["companies"]["Acme Robotics"].startswith("greenhouse board, 1 open jobs")


JOBVITE = """<table><tbody><tr> <td class="jv-job-list-name"> <a href="/ninjaone/job/o3gEAfwh">Associate Product Manager</a>
 </td> <td class="jv-job-list-location"> Remote<span>,</span> Bengaluru, Karnataka </td> </tr>
<tr> <td class="jv-job-list-name"> <a href="/ninjaone/job/ohAJAfwU">Senior Accountant</a> </td>
 <td class="jv-job-list-location"> Austin, TX </td> </tr></tbody></table>"""


def test_jobvite_boards(monkeypatch):
    assert S.parse_ats_url("https://jobs.jobvite.com/ninjaone/job/o3gEAfwh")["key"] == "jobvite:ninjaone"
    assert S.parse_ats_url("https://jobs.jobvite.com/__assets__/scripts/x.js") is None
    monkeypatch.setattr(S, "_get_text", lambda url, params=None, timeout=20:
                        JOBVITE if url == "https://jobs.jobvite.com/ninjaone/jobs" else _no_network())
    rows = S._jobvite("ninjaone", "NinjaOne")
    assert rows[0] == {"source": "NinjaOne careers (Jobvite)", "title": "Associate Product Manager", "company": "NinjaOne",
                       "location": "Remote, Bengaluru, Karnataka", "url": "https://jobs.jobvite.com/ninjaone/job/o3gEAfwh",
                       "ats": "jobvite", "direct": True}
    S.learn({"ats": "jobvite", "slug": "ninjaone", "key": "jobvite:ninjaone"}, company="NinjaOne")
    monkeypatch.setattr(J, "_board", lambda *a, **k: [])
    found = J._source_tasks("product manager", ["India"], True, "entry", 30, {}, [])["companies"]()
    assert [r["title"] for r in found] == ["Associate Product Manager"]                    # learned board, role filtered
    assert J.apply_via(found[0]) == "company form, usually no login (Jobvite)"


def test_job_details_for_workday_and_smartrecruiters(monkeypatch):
    def get_json(url, params=None, timeout=15):
        if "myworkdayjobs" in url:
            assert url == "https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/job/Bangalore/PM_R1"
            return {"jobPostingInfo": {"title": "Product Manager", "jobDescription": "<p>5+ years of product experience</p>",
                                       "location": "Bangalore", "startDate": "2026-09-18"},
                    "hiringOrganization": {"name": "Adobe Systems India Pvt Ltd"}}
        if "smartrecruiters" in url:
            return {"name": "PM", "company": {"name": "ServiceNow"}, "postingUrl": "https://jobs.smartrecruiters.com/ServiceNow/7",
                    "applyUrl": "https://jobs.smartrecruiters.com/ServiceNow/7?oga=true",
                    "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": "<p>Build things</p>"}}}}
        raise AssertionError(url)
    monkeypatch.setattr(S, "_get_json", get_json)
    out = json.loads(run_tool("get_job_details", {"url": "https://adobe.wd5.myworkdayjobs.com/external_experienced/job/Bangalore/PM_R1"}))
    assert out["company"] == "Adobe" and "5+ years" in out["description"] and out["apply_url"].endswith("/PM_R1/apply")
    assert "Create Account" in out["how_to_apply"]
    sr = S.details("https://jobs.smartrecruiters.com/ServiceNow/744000153614183")
    assert sr["apply_url"].endswith("?oga=true") and "Build things" in sr["description"]


# ------------------------------------------------------------------ find_jobs: ranking, LinkedIn fallback, company types
def _all_sources(monkeypatch, rows, linkedin=()):
    called = []
    for mod in (J, S, F):
        for name in [n for n in dir(mod) if n.startswith("src_")]:
            monkeypatch.setattr(mod, name, lambda *a, **k: [])
    monkeypatch.setattr(S, "src_workday", lambda *a, **k: [dict(r) for r in rows])
    monkeypatch.setattr(J, "src_linkedin", lambda *a, **k: called.append("linkedin") or [dict(r) for r in linkedin])
    return called


WD = "https://acme.wd5.myworkdayjobs.com/Ext/job/Pune/"


def test_find_jobs_company_sites_first_linkedin_only_as_fallback(monkeypatch):
    rows = [{"source": "Acme careers (Workday)", "title": "Associate Product Manager", "company": "Acme", "location": "Pune, India",
             "posted": _days_ago(2), "url": WD + "APM_1", "ats": "workday", "direct": True, "ctype": "product"},
            {"source": "Acme careers (Workday)", "title": "Senior Product Manager", "company": "Acme", "location": "Pune, India",
             "posted": _days_ago(2), "url": WD + "PM_2", "ats": "workday", "direct": True, "ctype": "product"}]
    linkedin = [{"source": "LinkedIn", "title": "Associate Product Manager", "company": "Acme", "location": "Pune, India",
                 "posted": _days_ago(1), "url": "https://www.linkedin.com/jobs/view/1"},
                {"source": "LinkedIn", "title": "Associate Product Manager", "company": "Beta", "location": "Pune, India",
                 "posted": _days_ago(1), "url": "https://www.linkedin.com/jobs/view/2"}]
    called = _all_sources(monkeypatch, rows, linkedin)
    monkeypatch.setattr(S, "details", lambda url: {"description": "You need 6+ years of product management experience."}
                        if url.endswith("PM_2") else None)
    out = J.find_jobs(query="associate product manager, product manager", locations=["India"], level="entry")
    assert called == ["linkedin"] and "fallback" in out["linkedin"]               # only 2 matches elsewhere
    acme = [j for j in out["jobs"] if j["company"] == "Acme" and j["title"] == "Associate Product Manager"]
    assert len(acme) == 1 and acme[0]["source"] == "Acme careers (Workday)"        # the company's own copy is kept
    beta = next(j for j in out["jobs"] if j["company"] == "Beta")
    assert acme[0]["match"] > beta["match"]                                        # LinkedIn ranks below company sites
    senior = next(j for j in out["jobs"] if j["title"] == "Senior Product Manager")
    assert "needs 6+ yrs" in senior["why"] or "senior role" in senior["why"]      # its description was read
    assert out["jobs"][0]["apply_via"].startswith("company site on Workday")
    assert "company career sites" in out["searched"]


def test_find_jobs_skips_linkedin_when_enough_and_filters_company_types(monkeypatch):
    rows = [{"source": f"Co{i} careers (Workday)", "title": "Product Manager", "company": f"Co{i}", "location": "Remote",
             "posted": _days_ago(3), "url": WD + f"PM_{i}", "ats": "workday", "direct": True, "ctype": "product"} for i in range(16)]
    rows += [{"source": "Accenture careers (Workday)", "title": "Product Manager", "company": "Accenture", "location": "Remote",
              "posted": _days_ago(3), "url": WD + "ACN", "ats": "workday", "direct": True, "ctype": "service"},
             {"source": "foundit", "title": "Product Manager", "company": "Xpheno Staffing Pvt Ltd", "location": "Remote",
              "posted": _days_ago(3), "url": "https://www.foundit.in/job/x", "ats": "foundit"}]
    called = _all_sources(monkeypatch, rows)
    monkeypatch.setattr(S, "details", lambda url: None)
    out = J.find_jobs(query="product manager", locations=["Remote"], level="entry", limit=40)
    assert called == [] and "linkedin" not in out                                   # enough found: no LinkedIn
    agency = next(j for j in out["jobs"] if j["company"].startswith("Xpheno"))
    assert agency["type"] == "staffing" and "agency" in agency["why"]
    products = J.find_jobs(query="product manager", locations=["Remote"], company_types=["product"], limit=40)
    names = {j["company"] for j in products["jobs"]}
    assert "Accenture" not in names and not any(n.startswith("Xpheno") for n in names) and "Co1" in names
    services = J.find_jobs(query="product manager", locations=["Remote"], company_types=["service"], limit=40)
    assert {j["company"] for j in services["jobs"]} == {"Accenture"}


def test_find_jobs_boosts_recently_funded_companies(monkeypatch):
    rows = [{"source": "Pandektes careers (Ashby)", "title": "Product Manager", "company": "Pandektes", "location": "Remote",
             "posted": _days_ago(3), "url": "https://jobs.ashbyhq.com/pandektes/1", "ats": "ashby", "direct": True},
            {"source": "Other careers (Ashby)", "title": "Product Manager", "company": "Other", "location": "Remote",
             "posted": _days_ago(3), "url": "https://jobs.ashbyhq.com/other/1", "ats": "ashby", "direct": True}]
    _all_sources(monkeypatch, rows)
    F._remember_for_tags([{"name": "Pandektes", "round": "Series A", "amount": "€13.5M (~$14.6M)", "date": _days_ago(1)}])
    out = J.find_jobs(query="product manager", locations=["Remote"], sources=["workday"])
    top = out["jobs"][0]
    assert top["company"] == "Pandektes" and "recently raised" in top["why"] and "Series A" in top["funding"]


# ------------------------------------------------------------------ the next session doesn't redo the last one
# The user: "it has to remember what companies it has applied and for next session to not go and do the same thing
# search same companies again".
ASHBY = "https://jobs.ashbyhq.com/"


def _pm(company, url, title="Product Manager"):
    return {"source": f"{company} careers (Ashby)", "title": title, "company": company, "location": "Remote",
            "posted": _days_ago(3), "url": url, "ats": "ashby", "direct": True}


def test_find_jobs_leaves_out_what_the_user_already_applied_to(monkeypatch):
    from karya import answers
    monkeypatch.setattr(answers, "RECENT_USER", ["find product manager jobs"])
    northwind = ASHBY + "northwind/0a1b2c3d-1111-4222-8333-444455556666"
    rows = [_pm("Northwind", northwind), _pm("Northwind", ASHBY + "northwind/11111111-2222-3333-4444-555555555555", "Product Lead"),
            _pm("Contoso AI", ASHBY + "contoso/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),        # same board, other name
            _pm("Fresh Co", ASHBY + "freshco/99999999-8888-7777-6666-555555555555")]
    _all_sources(monkeypatch, rows)
    monkeypatch.setattr(S, "details", lambda url: None)
    J.track_application("Northwind", "Product Manager (Payments)", northwind, "applied")
    J.track_application("Contoso", "Product Manager", ASHBY + "contoso/0f0e0d0c-aaaa-4bbb-8ccc-ddddeeeeffff", "applied")
    J.track_application("Saved Inc", "Product Manager", ASHBY + "saved/1", "saved")          # only saved: not applied
    out = J.find_jobs(query="product manager, product lead", locations=["Remote"], sources=["workday"])
    assert [j["company"] for j in out["jobs"]] == ["Fresh Co"]
    note = out["already_applied"]
    assert "1 job(s) the user already applied to" in note and "Northwind" in note and "Contoso" in note
    assert "include_applied_companies=true" in note
    # asked for: the companies come back, but the job already applied to never does
    again = J.find_jobs(query="product manager, product lead", locations=["Remote"], sources=["workday"],
                        include_applied_companies=True)
    urls = {j["url"] for j in again["jobs"]}
    assert northwind not in urls and len(urls) == 3
    # naming the company in the request also shows it
    monkeypatch.setattr(answers, "RECENT_USER", ["show me Contoso's product jobs"])
    named = J.find_jobs(query="product manager", locations=["Remote"], sources=["workday"])
    assert {j["company"] for j in named["jobs"]} == {"Contoso AI", "Fresh Co"}


def test_old_applications_only_block_the_same_job(monkeypatch):
    J.track_application("Tailspin", "Product Manager I", "https://jobs.lever.co/tailspin/8623c195-f912-4d87-952f-7114cd258413")
    data = J.applications_store.load()
    data["applications"][-1]["created"] = "2025-01-02 10:00"                       # long ago
    J.applications_store.save(data)
    memory = J.applied_memory()
    assert "tailspin" not in memory["companies"]                                      # the company is fair game again
    other = {"company": "Tailspin", "url": "https://jobs.lever.co/tailspin/00000000-1111-2222-3333-444444444444"}
    kept, hidden = J.skip_applied([other], memory)
    assert kept == [other] and hidden == {"jobs": 0, "companies": {}}
    same = {"company": "Tailspin", "url": "https://jobs.lever.co/tailspin/8623c195-f912-4d87-952f-7114cd258413/apply"}
    assert J.applied_before(same, memory)["role"] == "Product Manager I"           # but never the same job twice


def test_submit_is_blocked_for_a_job_already_in_the_tracker(monkeypatch):
    from karya.tools import browser
    J.track_application("Northwind", "Product Manager", ASHBY + "northwind/0a1b2c3d-1111-4222-8333-444455556666", "applied")

    class Fake(browser.BrowserSession):
        def call(self, fn, *a):
            return fn(*a)

        def form_check(self, element_id):
            return {"empty": []}
    fake = Fake()
    fake.url = ASHBY + "northwind/0a1b2c3d-1111-4222-8333-444455556666/application"
    fake.items = {4: {"id": 4, "tag": "button", "label": "Submit Application"}}
    monkeypatch.setattr(browser, "_current", lambda: fake)
    stop = browser._click_precheck({"element_id": 4})
    assert stop.startswith("NOT CLICKED: the user already applied") and "Northwind" in stop
    fake.url = ASHBY + "freshco/99999999-8888-7777-6666-555555555555/application"     # a new job: fine
    assert browser._click_precheck({"element_id": 4}) is None


def test_funded_companies_leave_out_applied_ones(monkeypatch):
    companies = [{"name": "Northwind", "round": "Series A", "date": _days_ago(5), "score": 9, "why": []},
                 {"name": "Fresh Co", "round": "Seed", "date": _days_ago(5), "score": 5, "why": []}]
    monkeypatch.setattr(F, "funded_companies", lambda *a, **k: [dict(c) for c in companies])
    monkeypatch.setattr(F, "rank_companies", lambda cs, *a: sorted(cs, key=lambda c: -c["score"]))
    J.track_application("Northwind", "Product Manager", ASHBY + "northwind/0a1b2c3d-1111-4222-8333-444455556666", "applied")
    out = F.find_funded_companies(query="product manager", with_jobs=False)
    assert [c["company"] for c in out["companies"]] == ["Fresh Co"] and "Northwind" in out["already_applied"]


def test_view_links_need_no_approval():
    from karya.registry import CRITICAL, SAFE
    from karya.tools import browser
    for label in ("View post", "Show 35 posts", "See all comments", "See new posts", "View application"):
        assert browser.classify_click({"tag": "a"}, label, "https://www.linkedin.com/feed/") == SAFE, label
    for label in ("Post", "Repost", "Send", "Submit application", "Delete post"):
        assert browser.classify_click({"tag": "button"}, label, "https://www.linkedin.com/feed/") == CRITICAL, label


# ------------------------------------------------------------------ funding news
HEADLINES = [
    ("At 19, founder raises $11M for Ghost, maker of a $3,499 computer for personal AI", "Ghost", "$11M", None),
    ("a16z-backed EliseAI raises $350M, doubles valuation to $4B", "EliseAI", "$350M", None),
    ("Exclusive: Homeward Raises $120M To Help Homeowners Buy And Sell More Quickly", "Homeward", "$120M", None),
    ("Namespace raises $42M Series B, seven months after Series A", "Namespace", "$42M", "Series B"),
    ("Quantum computing and AI firm QpiAI secures Rs 50 Cr debt financing from InnoVen Capital", "QpiAI", "₹50 Cr (~$6M)", "Debt"),
    ("Exclusive: Danish legal research startup Pandektes raises €13.5M Series A", "Pandektes", "€13.5M (~$14.6M)", "Series A"),
    ("Copenhagen’s Pandektes raises €13.5 million to build the data infrastructure", "Pandektes", "€13.5 million (~$14.6M)", None),
    ("Former Livspace executives’ startup Gravity raises $15 Mn led by Info Edge Ventures and 3one4 Capital", "Gravity", "$15 Mn", None),
    ("Physical AI startup SiMa.ai raises $150 million in Series C funding at $1.45 billion valuation", "SiMa.ai", "$150 million", "Series C"),
    ("Egyptian fintech startup Paymob raises $35m pre-Series C funding round", "Paymob", "$35m", "Pre-Series C"),
    ("OneByZero raises US$20 mil Series A led by Jungle Ventures", "OneByZero", "US$20 mil", "Series A"),
    ("Unveilr AI raises pre-seed funding from AJVC at Rs 16.7 crore valuation", "Unveilr AI", None, "Pre-seed"),
]
NOT_ONE_COMPANY = [
    "Two Google alumni raise $11.3M to back AI startups that enterprises will actually pay for",
    "Exclusive: Ex-Tesla team raises $12.5M to put supply chains on autopilot",
    "Arlington maritime startup raises $140M in Series B funding",
    "From Simple Energy To Arivihan — Indian Startups Raised Over $233 Mn This Week",
    "Sequoia closes $950M for its third fund",
    "Acme wins $20M contract from the Navy",
]


@pytest.mark.parametrize("title,name,amount,round_", HEADLINES)
def test_funding_headlines(title, name, amount, round_):
    got = F.parse_headline(title)
    assert got and got["name"] == name and got["amount"] == amount and got["round"] == round_


@pytest.mark.parametrize("title", NOT_ONE_COMPANY)
def test_not_a_company_raising(title):
    assert F.parse_headline(title) is None


def test_funding_details():
    assert F.parse_headline(HEADLINES[5][0])["country"] == "denmark"
    assert F.parse_headline(HEADLINES[9][0])["country"] == "egypt"
    assert F.parse_headline(HEADLINES[7][0])["investors"] == "Info Edge Ventures and 3one4 Capital"
    assert F.parse_headline(HEADLINES[4][0])["investors"] == "InnoVen Capital"
    assert "Fintech" in F.parse_headline(HEADLINES[9][0])["sectors"]


def _rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{format_datetime(d)}</pubDate>{s}</item>"
                   for t, l, d, s in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'.encode()


class _Resp:
    def __init__(self, content):
        self.content = content
        self.text = content.decode()


def test_funded_companies_from_news_and_yc(monkeypatch):
    now = datetime.now(timezone.utc)
    season = ["winter", "winter", "winter", "spring", "spring", "summer", "summer", "summer", "fall", "fall", "fall", "fall"][now.month - 1]
    batch = f"{season.title()} {now.year}"

    def get(url, params=None, timeout=15, headers=None):
        if "news.google.com" in url:
            return _Resp(_rss([("Namespace raises $42M Series B - TechCrunch", "https://n/1", now - timedelta(days=2),
                                "<source url='https://techcrunch.com'>TechCrunch</source>"),
                               ("Gravity raises $15 Mn led by Info Edge Ventures - Entrackr", "https://n/2", now - timedelta(days=3),
                                "<source url='https://entrackr.com'>Entrackr</source>"),
                               ("Oldco raises $9M seed", "https://n/3", now - timedelta(days=80), "")]))
        if "inc42" in url:
            return _Resp(_rss([("Zaperon Raises Rs 7 Cr In Seed Round Led By Inflection Point Ventures", "https://i/1",
                                now - timedelta(days=1), "")]))
        return _Resp(_rss([]))

    def get_json(url, params=None, timeout=15):
        if url.endswith("meta.json"):
            return {"batches": {"b": {"name": batch, "api": "https://yc-oss.github.io/api/batches/b.json"}}}
        return [{"name": "Acme AI", "isHiring": True, "one_liner": "AI agents for banks", "regions": ["India"], "slug": "acme-ai"},
                {"name": "Quiet Co", "isHiring": False, "slug": "quiet"}]
    monkeypatch.setattr(S, "_get", get)
    monkeypatch.setattr(S, "_get_json", get_json)
    found = {c["name"]: c for c in F.funded_companies(30)}
    assert {"Namespace", "Gravity", "Zaperon", "Acme AI"} <= set(found) and "Oldco" not in found and "Quiet Co" not in found
    assert found["Gravity"]["country"] == "india" and found["Zaperon"]["country"] == "india"   # Indian outlets
    assert found["Zaperon"]["investors"] == "Inflection Point Ventures" and found["Acme AI"]["yc"]
    ranked = F.rank_companies(list(found.values()), {"india"}, ["fintech", "AI"])
    assert ranked[0]["name"] in ("Gravity", "Zaperon") and "in India" in "; ".join(ranked[0]["why"])


def test_find_funded_companies_lists_their_jobs_with_ids(monkeypatch):
    monkeypatch.setattr(F, "funded_companies", lambda *a, **k: [
        {"name": "Gravity", "round": "Series A", "amount": "$15 Mn", "usd": 15e6, "date": _days_ago(2), "country": "india",
         "sectors": ["HR / work"], "news": "https://n/2", "links": ["https://n/2"]},
        {"name": "Faraway", "round": "Seed", "amount": "$2M", "usd": 2e6, "date": _days_ago(20), "country": "united states",
         "sectors": [], "news": "https://n/9", "links": ["https://n/9"]}])
    board = {"ats": "lever", "slug": "gravity", "key": "lever:gravity", "url": "https://jobs.lever.co/gravity", "company": "Gravity"}
    monkeypatch.setattr(S, "resolve_company", lambda name, **k: board if name == "Gravity" else None)
    monkeypatch.setattr(S, "fetch_board", lambda info, role="", places=(): [
        {"source": "gravity careers (Lever)", "title": "Associate Product Manager", "company": "Gravity",
         "location": "Bengaluru, India", "url": "https://jobs.lever.co/gravity/1", "ats": "lever", "direct": True},
        {"source": "gravity careers (Lever)", "title": "Sales Lead", "company": "Gravity", "location": "Bengaluru, India",
         "url": "https://jobs.lever.co/gravity/2", "ats": "lever", "direct": True}])
    out = json.loads(run_tool("find_funded_companies", {"query": "product manager", "countries": ["India"]}))
    assert [c["company"] for c in out["companies"]] == ["Gravity"]                  # India only, as asked
    gravity = out["companies"][0]
    assert gravity["open_roles"] == 2 and "1 open role fit you" in gravity["why"] and gravity["careers"].endswith("/gravity")
    job_id = gravity["jobs"][0]["id"]
    assert job_id.startswith("J") and J.job_by_id(job_id)["url"] == "https://jobs.lever.co/gravity/1"   # choose_jobs works
    anywhere = json.loads(run_tool("find_funded_companies", {"query": "product manager", "with_jobs": False}))
    assert {c["company"] for c in anywhere["companies"]} == {"Gravity", "Faraway"}
