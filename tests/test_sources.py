"""Only the employer's own listings are trusted."""
import pytest

from jobscout.sources import ATS, DENIED, EMPLOYER, PUBLIC, check_source, classify


@pytest.mark.parametrize("url,company,expected", [
    ("https://boards.greenhouse.io/acme/jobs/4123", "Acme", ATS),
    ("https://job-boards.greenhouse.io/acme/jobs/4123", "Acme", ATS),
    ("https://jobs.lever.co/globex/abc", "Globex", ATS),
    ("https://jobs.ashbyhq.com/initech/xyz", "Initech", ATS),
    ("https://acme.wd1.myworkdayjobs.com/en-US/Careers/job/1", "Acme", ATS),
    ("https://jobs.smartrecruiters.com/Acme/7", "Acme", ATS),
    ("https://careers.acme.com/job/123", "Acme", EMPLOYER),
    ("https://www.some-lab.gov/careers/jobs/1", "Some Lab", PUBLIC),
    ("https://university.edu/jobs/5", "University", PUBLIC),
])
def test_trusted(url, company, expected):
    accepted, source_class, _ = check_source(url, company)
    assert accepted and source_class == expected


@pytest.mark.parametrize("url", [
    "https://www.indeed.com/viewjob?jk=abc",
    "https://www.linkedin.com/jobs/view/3024174855",
    "https://www.ziprecruiter.com/c/Acme/Job/x",
    "https://www.glassdoor.com/job-listing/x",
    "https://www.dice.com/jobs/detail/x",
    "https://builtin.com/job/x",
    "https://wellfound.com/jobs/1",
    "https://www.roberthalf.com/jobs/1",
    "https://weworkremotely.com/remote-jobs/1",
])
def test_aggregators_and_staffing_are_denied(url):
    accepted, source_class, _ = check_source(url, "Acme")
    assert not accepted and source_class == DENIED


def test_an_aggregator_path_cannot_impersonate_the_employer():
    # jobs.indeed.com/acme/... is still Indeed.
    accepted, source_class, _ = check_source("https://jobs.indeed.com/acme/engineer", "Acme")
    assert not accepted and source_class == DENIED


def test_a_lookalike_domain_is_not_the_employer():
    accepted, _, _ = check_source("https://totally-legit-jobs.biz/acme/1", "Acme")
    assert not accepted


def test_unknown_sites_are_rejected_not_merely_flagged():
    accepted, source_class, reason = check_source("https://randomboard.example/job/1", "Acme")
    assert not accepted
    assert source_class == "unknown"
    assert "cannot be trusted" in reason


def test_acronym_domains_count_as_the_employer():
    assert classify("https://careers.lanl.example/jobs/1",
                    "Los Alamos National Laboratory")[0] == EMPLOYER
