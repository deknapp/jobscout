"""Reading ATS boards straight from their public JSON, with no model involved.

These parse recorded API shapes; nothing here touches the network.
"""
import datetime as dt

import pytest

from jobscout import fetchers

GREENHOUSE = {"jobs": [
    {"title": "Senior Data Engineer",
     "location": {"name": "Albuquerque, NM"},
     "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
     "updated_at": "2026-08-30T17:04:11-04:00",
     "content": "&lt;p&gt;Own the &lt;b&gt;data platform&lt;/b&gt;.&lt;/p&gt;"},
]}

LEVER = [
    {"text": "Platform Engineer",
     "categories": {"location": "Santa Fe, NM"},
     "workplaceType": "remote",
     "hostedUrl": "https://jobs.lever.co/acme/abc",
     "createdAt": 1788242400000,
     "descriptionPlain": "Build the deployment platform."},
]

ASHBY = {"jobs": [
    {"title": "Research Engineer", "location": "Remote - US",
     "secondaryLocations": [{"location": "Austin"}],
     "isRemote": True, "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/acme/xyz",
     "publishedAt": "2026-09-01T00:00:00.000Z",
     "descriptionPlain": "Applied research."},
    {"title": "Hidden role", "location": "Nowhere", "isListed": False},
]}

SMART = {"content": [
    {"id": "744000137413079", "name": "Analytics Engineer",
     "location": {"fullLocation": "Albuquerque, NM, US", "remote": False},
     "releasedDate": "2026-08-28T09:50:21.127Z"},
]}


def _patch(monkeypatch, payload):
    monkeypatch.setattr(fetchers, "_get_json", lambda url: payload)


def test_greenhouse(monkeypatch):
    _patch(monkeypatch, GREENHOUSE)
    result = fetchers.fetch("Acme", "https://boards.greenhouse.io/acme")
    assert result.ok and result.ats == "Greenhouse"
    job = result.postings[0]
    assert job.title == "Senior Data Engineer"
    assert job.location == "Albuquerque, NM"
    assert job.posted == "2026-08-30"
    assert "data platform" in job.summary  # HTML stripped and unescaped
    assert "<" not in job.summary


def test_lever_epoch_millis_and_workplace_type(monkeypatch):
    _patch(monkeypatch, LEVER)
    result = fetchers.fetch("Acme", "https://jobs.lever.co/acme")
    job = result.postings[0]
    assert job.posted == dt.date(2026, 9, 1).isoformat()
    # The workplace type is folded into the location so the filter can see it.
    assert job.location == "Santa Fe, NM (remote)"


def test_ashby_skips_unlisted_and_keeps_secondary_locations(monkeypatch):
    _patch(monkeypatch, ASHBY)
    result = fetchers.fetch("Acme", "https://jobs.ashbyhq.com/acme")
    assert len(result.postings) == 1
    assert "Remote - US" in result.postings[0].location
    assert "Austin" in result.postings[0].location


def test_smartrecruiters_builds_the_posting_url(monkeypatch):
    _patch(monkeypatch, SMART)
    result = fetchers.fetch("Acme", "https://jobs.smartrecruiters.com/acme")
    assert result.postings[0].url.endswith("/acme/744000137413079")


def test_an_unsupported_host_returns_none_so_the_agent_takes_over():
    assert fetchers.fetch("Acme", "https://careers.acme.com/jobs") is None
    assert not fetchers.supports("https://careers.acme.com/jobs")
    assert fetchers.supports("https://boards.greenhouse.io/acme")


def test_a_broken_api_reports_instead_of_raising(monkeypatch):
    def boom(url):
        raise OSError("connection reset")

    monkeypatch.setattr(fetchers, "_get_json", boom)
    result = fetchers.fetch("Acme", "https://jobs.lever.co/acme")
    assert result is not None and not result.ok
    assert "unreachable" in result.note


# --- Workday ---------------------------------------------------------------

WORKDAY_SEARCH = {"total": 2, "jobPostings": [
    {"title": "Senior Data Engineer", "locationsText": "Albuquerque, NM",
     "externalPath": "/job/Albuquerque-NM/Senior-Data-Engineer_R-1",
     "postedOn": "Posted 4 Days Ago"},
    {"title": "Software Engineer", "locationsText": "3 Locations",
     "externalPath": "/job/Elsewhere/Software-Engineer_R-2",
     "postedOn": "Posted 30+ Days Ago"},
]}

WORKDAY_DETAIL = {"jobPostingInfo": {
    "title": "Software Engineer",
    "location": "Los Alamos, NM",
    "additionalLocations": ["Denver, CO"],
    "startDate": "2026-08-29",
    "externalUrl": "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/x",
    "jobDescription": "<p>Build things.</p>",
}}


def test_workday_urls_are_parsed_into_a_cxs_endpoint():
    from jobscout.fetchers import _workday_parts

    assert _workday_parts("https://leidos.wd5.myworkdayjobs.com/External") == (
        "leidos.wd5.myworkdayjobs.com", "leidos", "External")
    # The locale segment is not the site name.
    assert _workday_parts("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/1")[2] == "Careers"


@pytest.mark.parametrize("text,days_ago", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 12 Days Ago", 12),
    ("Posted 30+ Days Ago", 31), ("Posted 2 Months Ago", 60),
    ("Posted 1 Year Ago", 365),
])
def test_workday_relative_dates_become_real_ones(text, days_ago):
    from jobscout.fetchers import _relative_date

    today = dt.date(2026, 9, 4)
    assert _relative_date(text, today) == (today - dt.timedelta(days=days_ago)).isoformat()


def test_workday_resolves_a_multi_location_posting(monkeypatch):
    """"3 Locations" can be hiding the only location that matters."""
    monkeypatch.setattr(fetchers, "_post_json", lambda url, body: WORKDAY_SEARCH)
    monkeypatch.setattr(fetchers, "_get_json", lambda url: WORKDAY_DETAIL)

    result = fetchers.fetch("Acme", "https://acme.wd5.myworkdayjobs.com/Careers",
                            context={"titles": ["Data Engineer"]})
    by_title = {p.title: p for p in result.postings}

    assert by_title["Senior Data Engineer"].location == "Albuquerque, NM"
    resolved = by_title["Software Engineer"]
    assert "Los Alamos, NM" in resolved.location
    assert resolved.posted == "2026-08-29"      # the real date beats "30+ days"
    assert "Build things" in resolved.summary
