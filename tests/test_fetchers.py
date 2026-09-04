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
