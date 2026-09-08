"""Board discovery without a model, and without a network.

Every test here uses a fake getter. The point of this module is to replace
paid model calls with HTTP requests; a test suite that made those requests for
real would be slow, flaky, and rude to the boards being probed.
"""
from __future__ import annotations

import json

from jobscout.discover import (BoardGuess, crawl_careers, find_board,
                               is_specific, probe_ats, slugs_for)


def fake(pages: dict):
    """A getter backed by a dict of url -> (status, body). Anything not
    listed 404s, which is what an unknown ATS slug really does."""
    calls = []

    def get(url: str):
        calls.append(url)
        return pages.get(url, (404, ""))

    get.calls = calls  # type: ignore[attr-defined]
    return get


def greenhouse(slug: str, name: str, jobs: int = 3) -> dict:
    return {
        "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug:
            (200, json.dumps({"jobs": [{"id": i} for i in range(jobs)]})),
        "https://boards-api.greenhouse.io/v1/boards/%s" % slug:
            (200, json.dumps({"name": name})),
    }


# --- slugs -----------------------------------------------------------------

def test_legal_suffixes_are_not_part_of_a_slug():
    assert slugs_for("Kitware, Inc.") == ["kitware"]
    assert "enthought" in slugs_for("Enthought, Inc.")


def test_the_whole_name_is_tried_before_the_first_word():
    assert slugs_for("Descartes Labs") == ["descarteslabs", "descartes-labs", "descartes"]


def test_a_truncation_is_not_specific():
    assert is_specific("descarteslabs", "Descartes Labs")
    assert is_specific("descartes-labs", "Descartes Labs")
    assert not is_specific("descartes", "Descartes Labs")


def test_a_one_word_name_has_nothing_to_truncate():
    assert is_specific("kitware", "Kitware, Inc.")


# --- probing ---------------------------------------------------------------

def test_a_confirmed_greenhouse_board_is_accepted():
    get = fake(greenhouse("kairospower", "Kairos Power", jobs=22))
    guess = probe_ats("Kairos Power", get)
    assert guess and guess.ats == "Greenhouse"
    assert guess.url == "https://boards.greenhouse.io/kairospower"
    assert guess.how == "named" and guess.jobs_seen == 22


def test_a_greenhouse_board_with_someone_elses_name_is_refused():
    """The regression that motivated the name check. `General` matched a
    Greenhouse board on the first probe; the board is called "General
    Interest" and belongs to nobody. Accepting it would have cached another
    party's postings against this employer permanently."""
    get = fake(greenhouse("general", "General Interest", jobs=1))
    assert probe_ats("General", get) is None


def test_a_truncated_slug_is_refused_where_no_name_can_confirm_it():
    """Lever does not say whose board it is, so `descartes` finding one
    proves nothing about Descartes Labs."""
    get = fake({"https://api.lever.co/v0/postings/descartes?mode=json":
                (200, json.dumps([{"id": "a"}]))})
    assert probe_ats("Descartes Labs", get) is None


def test_a_specific_slug_is_accepted_on_lever():
    get = fake({"https://api.lever.co/v0/postings/postera?mode=json":
                (200, json.dumps([{"id": "a"}, {"id": "b"}]))})
    guess = probe_ats("PostEra", get)
    assert guess and guess.ats == "Lever" and guess.jobs_seen == 2


def test_an_empty_board_is_not_a_board():
    """A 200 with no postings is an ATS account nobody uses. Recording it
    would mark the employer resolved and stop anyone ever looking again."""
    get = fake({"https://api.ashbyhq.com/posting-api/job-board/acme":
                (200, json.dumps({"jobs": []}))})
    assert probe_ats("Acme", get) is None


def test_probing_gives_up_rather_than_hammering_the_boards():
    get = fake({})
    probe_ats("One Two Three", get)
    assert len(get.calls) <= 14


def test_nothing_found_means_ask_a_model_not_guess():
    assert probe_ats("Booz Allen", fake({})) is None


# --- crawling --------------------------------------------------------------

HOME = "https://acme.example"


def test_a_careers_link_straight_to_an_ats_is_taken():
    get = fake({HOME: (200, '<a href="https://boards.greenhouse.io/acme">Careers</a>')})
    guess = crawl_careers(HOME, get)
    assert guess and guess.ats == "Greenhouse" and guess.how == "link"


def test_the_ats_is_followed_one_hop_through_the_careers_page():
    get = fake({
        HOME: (200, '<a href="/careers">Join us</a>'),
        HOME + "/careers": (200, '<a href="https://jobs.lever.co/acme">See openings</a>'),
    })
    guess = crawl_careers(HOME, get)
    assert guess and guess.ats == "Lever"
    assert guess.url == "https://jobs.lever.co/acme"


def test_a_careers_page_with_no_rented_board_is_still_a_board():
    get = fake({
        HOME: (200, '<a href="/careers">Careers</a>'),
        HOME + "/careers": (200, "<p>Email us your CV</p>"),
    })
    guess = crawl_careers(HOME, get)
    assert guess and guess.ats == "in-house" and guess.url == HOME + "/careers"


def test_a_site_that_does_not_load_yields_nothing():
    assert crawl_careers(HOME, fake({})) is None
    assert crawl_careers("", fake({})) is None


def test_find_board_probes_first_then_crawls():
    """Probing is one request against a known API; crawling is fetching
    someone's homepage and reading it. Order matters for both cost and
    politeness."""
    pages = dict(greenhouse("acme", "Acme"))
    pages[HOME] = (200, '<a href="https://jobs.lever.co/acme">Careers</a>')
    get = fake(pages)
    guess = find_board("Acme", HOME, get)
    assert guess and guess.ats == "Greenhouse", "should not have needed the homepage"
    assert HOME not in get.calls


def test_find_board_falls_back_to_the_site_when_probing_fails():
    get = fake({HOME: (200, '<a href="https://jobs.ashbyhq.com/acme">Careers</a>')})
    guess = find_board("Acme", HOME, get)
    assert guess and guess.ats == "Ashby" and guess.how == "link"


# --- the saving is only real if the model is not called ---------------------

def test_resolve_board_does_not_call_the_model_when_probing_worked(monkeypatch):
    """The whole point. If the free path finds the board and the paid path
    runs anyway, nothing has been saved."""
    from jobscout import agents, discover as disc
    from jobscout.companies import Company

    monkeypatch.setattr(disc, "find_board", lambda name, homepage="", **kw: BoardGuess(
        url="https://boards.greenhouse.io/acme", ats="Greenhouse", slug="acme",
        how="named", jobs_seen=7, probes=2))

    class Exploding:
        def ask_json(self, *a, **k):
            raise AssertionError("the model must not be asked once probing succeeded")

    out = agents.resolve_board(Exploding(), Company(name="Acme"))
    assert out["careers_url"] == "https://boards.greenhouse.io/acme"
    assert out["ats"] == "Greenhouse"
    assert "without a model call" in out["note"]


def test_resolve_board_still_asks_the_model_when_probing_fails(monkeypatch):
    from jobscout import agents, discover as disc
    from jobscout.companies import Company

    monkeypatch.setattr(disc, "find_board", lambda name, homepage="", **kw: None)
    asked = []

    class Recording:
        def ask_json(self, prompt, **k):
            asked.append(prompt)
            return {"careers_url": "https://acme.example/careers",
                    "ats": "in-house", "note": "found it"}

    out = agents.resolve_board(Recording(), Company(name="Booz Allen"))
    assert asked, "the paid path is the fallback, not dead code"
    assert out["ats"] == "in-house"
