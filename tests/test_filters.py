"""The hard location filter is the feature; these are its teeth."""
import datetime as dt

import pytest

from jobscout.config import LocationPolicy
from jobscout.filters import check_freshness, check_location, check_verified, dedupe
from jobscout.models import Posting

NM = LocationPolicy(
    allowed_states=["NM"],
    allowed_cities=["albuquerque", "santa fe", "los alamos", "rio rancho", "las cruces"],
    allow_remote=True,
    allow_hybrid=False,
).normalized()


@pytest.mark.parametrize("location,expected", [
    # in-state, in any working arrangement
    ("Albuquerque, NM", True),
    ("Los Alamos, New Mexico", True),
    ("Santa Fe, NM (hybrid, 2 days in office)", True),
    ("Remote within New Mexico", True),
    # genuinely remote
    ("Remote - US", True),
    ("Remote (USA)", True),
    ("Remote, United States", True),
    ("Fully remote", True),
    ("Seattle, WA or Remote", True),
    # remote in name only — the trap
    ("Remote (must reside in California)", False),
    ("Remote - EMEA", False),
    ("Remote: Canada", False),
    ("Remote — open to candidates in Texas and Colorado", False),
    # elsewhere
    ("Denver, CO", False),
    ("Austin, TX", False),
    ("Hybrid - Austin, TX (3 days in office)", False),
    ("London, United Kingdom", False),
    # unknowable
    ("", False),
])
def test_location_gate(location, expected):
    accepted, _mode, _reason = check_location(Posting(location=location), NM)
    assert accepted is expected, location


def test_remote_fenced_to_an_allowed_state_is_accepted():
    posting = Posting(location="Remote (US) — open to candidates in Texas, Colorado and New Mexico")
    accepted, mode, _ = check_location(posting, NM)
    assert accepted and mode == "remote"


def test_remote_can_be_switched_off():
    policy = LocationPolicy(allowed_states=["NM"], allow_remote=False).normalized()
    accepted, _, reason = check_location(Posting(location="Remote - US"), policy)
    assert not accepted and "policy" in reason


def test_a_two_letter_state_code_does_not_match_inside_a_word():
    # "NM" must not fire on "Wilmington" or similar.
    accepted, _, _ = check_location(Posting(location="Wilmington, DE"), NM)
    assert not accepted


def test_freshness():
    today = dt.date(2026, 9, 4)
    assert check_freshness(Posting(posted="2026-09-01"), 30, today)[0]
    assert not check_freshness(Posting(posted="2026-01-01"), 30, today)[0]


def test_undated_posting_needs_verification():
    assert not check_freshness(Posting(posted=""), 30)[0]
    assert check_freshness(Posting(posted="", verified="live"), 30)[0]


def test_only_verified_listings_survive():
    assert check_verified(Posting(verified="live"))[0]
    for status in ("dead", "mismatch", "unchecked"):
        assert not check_verified(Posting(verified=status))[0]


def test_dedupe_collapses_the_same_role_from_two_searches():
    postings = [
        Posting(company="Acme", title="Senior Data Engineer", url="https://boards.greenhouse.io/acme/jobs/1"),
        Posting(company="Acme Inc.", title="Data Engineer II", url="https://boards.greenhouse.io/acme/jobs/1?utm=x"),
        Posting(company="Globex", title="SRE", url="https://jobs.lever.co/globex/2"),
    ]
    assert len(dedupe(postings)) == 2


def test_only_your_own_decisions_hide_a_job():
    """Nothing else may withhold a role from someone trying to find work.

    Every other kind of rejection is a judgement made by this tool, often on a
    threshold the user can change, and it is still logged — but it never again
    silently removes a job from view.
    """
    import tempfile
    from pathlib import Path

    from jobscout.history import APPLIED, DISMISSED, DROPPED, History, RECOMMENDED

    path = Path(tempfile.mkdtemp()) / "history.jsonl"
    history = History(path)

    shown = Posting(company="Iambic", title="Software Engineer",
                    url="https://jobs.ashbyhq.com/iambic/1")
    stale = Posting(company="Iambic", title="ML Scientist",
                    url="https://jobs.ashbyhq.com/iambic/2")
    unchecked = Posting(company="Iambic", title="Platform Engineer",
                        url="https://jobs.ashbyhq.com/iambic/3")
    applied = Posting(company="Iambic", title="Research Engineer",
                      url="https://jobs.ashbyhq.com/iambic/4")
    dismissed = Posting(company="Iambic", title="Medical Writer",
                        url="https://jobs.ashbyhq.com/iambic/5")

    history.record(shown, RECOMMENDED)
    history.record(stale, DROPPED, "posted 78 days ago (limit 30)")
    history.record(unchecked, DROPPED, "not verified")
    history.record(applied, APPLIED)
    history.record(dismissed, DISMISSED)

    reloaded = History(path)
    assert not reloaded.seen_before(shown)[0]      # shown once is not "dealt with"
    assert not reloaded.seen_before(stale)[0]      # you can raise the age limit
    assert not reloaded.seen_before(unchecked)[0]  # our failure, not the job's
    assert reloaded.seen_before(applied)[0]        # you applied
    assert reloaded.seen_before(dismissed)[0]      # you said no
