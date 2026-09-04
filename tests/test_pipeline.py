"""End to end, offline, for free.

The whole pipeline runs against the mock backend, so this exercises the real
filtering and history logic on a realistic mix of postings: one good local role,
one genuinely remote role, one "remote" role fenced to another state, one from an
aggregator, and one that is months old.
"""
import datetime as dt
import json
import re

import pytest

from jobscout import fetchers, pipeline
from jobscout.config import LocationPolicy, Settings
from jobscout.llm import LLM, MockBackend
from jobscout.models import Posting

TODAY = dt.date(2026, 9, 4)
RECENT = (TODAY - dt.timedelta(days=3)).isoformat()
ANCIENT = (TODAY - dt.timedelta(days=400)).isoformat()

PROFILE = json.dumps({
    "headline": "Data engineer with scientific computing experience",
    "years_experience": 8,
    "seniority": "senior",
    "core_skills": ["python", "sql", "pipelines"],
    "domains": ["scientific computing"],
    "target_titles": ["Data Engineer", "Software Engineer"],
    "adjacent_titles": ["Platform Engineer"],
    "employer_types": ["national labs"],
    "differentiators": ["lab plus startup"],
    "seniority_floor": "Data Engineer",
    "avoid": [],
    "notes": "",
})

COMPANIES = json.dumps([
    {"name": "Aurora Instruments", "why": "instrument data pipelines",
     "presence": "Albuquerque, NM", "hiring_signal": "careers page lists openings"},
])

BOARD = json.dumps({"careers_url": "https://boards.greenhouse.io/aurora",
                    "ats": "Greenhouse", "note": "fetched, 12 roles listed"})

POSTINGS = json.dumps([
    {"title": "Senior Data Engineer", "location": "Albuquerque, NM",
     "url": "https://boards.greenhouse.io/aurora/jobs/1", "posted": RECENT,
     "salary": "$150k-$180k", "summary": "Own the instrument data platform."},
    {"title": "Platform Engineer", "location": "Remote - US",
     "url": "https://boards.greenhouse.io/aurora/jobs/2", "posted": RECENT,
     "salary": "", "summary": "Build the deployment platform."},
    {"title": "Staff Engineer", "location": "Remote (must reside in California)",
     "url": "https://boards.greenhouse.io/aurora/jobs/3", "posted": RECENT,
     "salary": "", "summary": "Lead the core team."},
    {"title": "Data Engineer", "location": "Albuquerque, NM",
     "url": "https://www.indeed.com/viewjob?jk=aurora99", "posted": RECENT,
     "salary": "", "summary": "Same role, scraped elsewhere."},
    {"title": "Analytics Engineer", "location": "Santa Fe, NM",
     "url": "https://boards.greenhouse.io/aurora/jobs/5", "posted": ANCIENT,
     "salary": "", "summary": "Long-expired listing."},
])


def _verify(prompt):
    """Answer as the live page would: echo back the claimed location."""
    match = re.search(r'located "([^"]*)"', prompt)
    return json.dumps({
        "status": "live",
        "actual_title": "",
        "actual_location": match.group(1) if match else "",
        "posted": RECENT,
        "closes": "",
        "note": "listing is open",
    })


def _rank(prompt):
    ids = re.findall(r'"id": "([0-9a-f]+)"', prompt)
    return json.dumps([
        {"id": job_id, "fit_score": 90 - index * 10,
         "rationale": "Matches the pipeline work in their resume.",
         "resembles": "their lab application", "concerns": "", "angle": "the platform work"}
        for index, job_id in enumerate(ids)
    ])


RESPONSES = {
    "Find the OFFICIAL page": BOARD,
    "Fetch this employer's job board": POSTINGS,
    "Fetch this URL and tell me": _verify,
    "Score each of these": _rank,
    "REAL employers": COMPANIES,
    "MATERIALS": PROFILE,
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    applications = tmp_path / "apps" / "Some Lab"
    applications.mkdir(parents=True)
    (applications / "resume_somelab.txt").write_text(
        "Nine years building data pipelines for scientific instruments.",
        encoding="utf-8")
    data = tmp_path / "state"

    backend = MockBackend(RESPONSES)
    monkeypatch.setattr(
        LLM, "from_settings",
        classmethod(lambda cls, s: LLM(backend, model_cheap="m", model_strong="M")))
    # No test touches the network: pretend this host has no ATS API, which
    # exercises the agent-driven fallback path.
    monkeypatch.setattr(fetchers, "fetch",
                        lambda company, url, context=None: None)

    return Settings(
        applications_dir=tmp_path / "apps",
        data_dir=data,
        backend="mock",
        max_age_days=30,
        max_results=10,
        company_target=1,
        location=LocationPolicy(
            allowed_states=["NM"],
            allowed_cities=["albuquerque", "santa fe"],
            allow_remote=True,
        ).normalized(),
    )


def test_a_full_run_keeps_only_real_local_fresh_roles(settings):
    result = pipeline.find(settings, today=TODAY)

    titles = sorted(p.title for p in result.recommended)
    assert titles == ["Platform Engineer", "Senior Data Engineer"]

    reasons = {p.title: p.rejected_reason for p in result.dropped}
    assert "restricted to california" in reasons["Staff Engineer"]
    assert "aggregator" in reasons["Data Engineer"]
    assert "limit 30" in reasons["Analytics Engineer"]


def test_the_report_carries_the_verified_details(settings):
    from jobscout import report

    result = pipeline.find(settings, today=TODAY)
    text = report.render(result.recommended, result.dropped, result.stats,
                         today=TODAY, location_summary=settings.location.summary())
    assert "Senior Data Engineer" in text
    assert "boards.greenhouse.io/aurora/jobs/1" in text
    assert "indeed.com/viewjob" not in text  # never offered as a link
    assert "Untrusted source" in text


def test_a_role_you_have_not_acted_on_keeps_being_offered(settings):
    """The opposite of the old behaviour, and deliberately so.

    A role shown yesterday and not applied to is still a role worth applying to.
    Only marking it applied or dismissed takes it out of circulation.
    """
    from jobscout.history import APPLIED, History

    first = pipeline.find(settings, today=TODAY)
    assert len(first.recommended) == 2

    second = pipeline.find(settings, today=TODAY + dt.timedelta(days=1))
    assert len(second.recommended) == 2, "showing it once is not dealing with it"

    history = History(settings.history_path)
    history.mark(first.recommended[0].id, APPLIED)

    third = pipeline.find(settings, today=TODAY + dt.timedelta(days=2))
    assert len(third.recommended) == 1
    assert third.stats.get("dropped_seen") == 1


def test_the_employer_registry_persists_between_runs(settings):
    from jobscout.companies import Registry

    pipeline.find(settings, today=TODAY)
    registry = Registry(settings.companies_path)
    company = registry.get("Aurora Instruments")
    assert company is not None
    assert company.careers_url == "https://boards.greenhouse.io/aurora"
    assert company.last_scanned == dt.date.today().isoformat()


def test_the_profile_is_written_once_and_reused(settings):
    pipeline.find(settings, today=TODAY)
    assert settings.profile_path.exists()
    profile = json.loads(settings.profile_path.read_text())
    assert profile["seniority"] == "senior"
    assert profile["applied_companies"] == ["Some Lab"]


def test_roles_from_a_board_api_are_not_re_verified(settings, monkeypatch):
    """A role on the board's live API is on the board. Spending a fetch to
    re-confirm that is pure waste, and the budget belongs to agent-reported
    postings, which genuinely might not exist."""
    from jobscout.fetchers import FetchResult

    api_roles = [
        Posting(company="Aurora Instruments", title="Senior Data Engineer",
                location="Albuquerque, NM",
                url="https://boards.greenhouse.io/aurora/jobs/1",
                posted=RECENT, source="Greenhouse"),
        Posting(company="Aurora Instruments", title="Staff Engineer",
                location="Remote (must reside in California)",
                url="https://boards.greenhouse.io/aurora/jobs/3",
                posted=RECENT, source="Greenhouse"),
    ]
    monkeypatch.setattr(
        fetchers, "fetch",
        lambda company, url, context=None: FetchResult(postings=list(api_roles),
                                                       ats="Greenhouse"))

    result = pipeline.find(settings, today=TODAY)

    assert [p.title for p in result.recommended] == ["Senior Data Engineer"]
    assert result.stats["live_from_api"] == 1
    # The California role never even reached the pipeline: the location gate ran
    # at the source, for free, on the whole board.
    assert result.stats["narrowed_at_source"] == 1

    backend = LLM.from_settings(settings).backend
    assert not any("Fetch this URL and tell me" in prompt
                   for prompt in backend.prompts)


def test_roles_are_published_as_each_employer_is_read(settings, monkeypatch):
    """A dashboard should fill in during a run, not stay blank until the end."""
    from jobscout.fetchers import FetchResult

    monkeypatch.setattr(
        fetchers, "fetch",
        lambda company, url, context=None: FetchResult(
            postings=[Posting(company="Aurora Instruments", title="Senior Data Engineer",
                              location="Albuquerque, NM",
                              url="https://boards.greenhouse.io/aurora/jobs/1",
                              posted=RECENT, source="Greenhouse")],
            ats="Greenhouse"))

    updates = []
    pipeline.find(settings, today=TODAY,
                  on_update=lambda postings: updates.append(
                      [(p.title, p.stage) for p in postings]))

    stages = [stage for batch in updates for _title, stage in batch]
    # Found first — before verification or scoring could possibly have run.
    assert stages[0] == "found"
    assert "scored" in stages
    assert stages.index("found") < stages.index("scored")


def test_free_boards_are_read_every_run_and_never_capped(settings, monkeypatch):
    """Capping free, instant board reads only means missing jobs."""
    from jobscout.companies import Company, RESOLVED, Registry
    from jobscout.fetchers import FetchResult

    registry = Registry(settings.companies_path)
    for index in range(25):
        registry.add(Company(name="ATS Employer %d" % index, status=RESOLVED,
                             careers_url="https://boards.greenhouse.io/e%d" % index,
                             last_scanned=TODAY.isoformat()))   # scanned today
    for index in range(5):
        registry.add(Company(name="Slow Employer %d" % index, status=RESOLVED,
                             careers_url="https://careers.slow%d.com/jobs" % index))
    registry.save()

    read = []
    monkeypatch.setattr(
        fetchers, "fetch",
        lambda company, url, context=None: (
            read.append(company) or FetchResult(postings=[], ats="Greenhouse"))
        if "greenhouse" in url else None)

    settings.max_scans_per_run = 2      # applies to the slow ones only
    settings.company_target = 0         # do not propose more
    pipeline.find(settings, today=TODAY)

    # All 25 API boards read despite being scanned today and despite the cap.
    assert len(read) == 25
