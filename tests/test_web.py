"""The local web app serves the board without any model calls."""
import datetime as dt
import json

import pytest

from jobscout.board import Board
from jobscout.config import LocationPolicy, Settings
from jobscout.history import APPLIED, History, RECOMMENDED
from jobscout.models import Posting
from jobscout.web import STATIC_DIR, App

TODAY = dt.date(2026, 9, 4)


@pytest.fixture
def app(tmp_path):
    settings = Settings(
        applications_dir=tmp_path / "apps",
        data_dir=tmp_path / "state",
        backend="mock",
        location=LocationPolicy(allowed_states=["NM"], allow_remote=True).normalized(),
    )
    settings.ensure_data_dir()

    postings = [
        Posting(company="Aurora", title="Senior Data Engineer",
                url="https://boards.greenhouse.io/aurora/jobs/1",
                location="Albuquerque, NM", work_mode="onsite",
                posted=(TODAY - dt.timedelta(days=2)).isoformat(),
                fit_score=88, likelihood=70, source="Greenhouse", verified="live"),
        Posting(company="Globex", title="Platform Engineer",
                url="https://jobs.lever.co/globex/2", location="Remote - US",
                work_mode="remote",
                posted=(TODAY - dt.timedelta(days=25)).isoformat(),
                fit_score=80, likelihood=30, source="Lever", verified="live"),
    ]
    board = Board(settings.board_path)
    board.merge(postings, today=TODAY)
    board.save()

    history = History(settings.history_path)
    for posting in postings:
        history.record(posting, RECOMMENDED, today=TODAY)

    return App(settings), postings


def test_the_payload_ranks_and_annotates_the_board(app):
    application, postings = app
    payload = application.board_payload()

    assert payload["counts"]["roles"] == 2
    assert [r["title"] for r in payload["roles"]][0] == "Senior Data Engineer"
    for role in payload["roles"]:
        assert role["composite"] > 0
        assert role["recency_score"] > 0
        assert role["status"] == RECOMMENDED
        assert "band" in role


def test_marking_a_role_applied_reaches_the_history(app):
    application, postings = app
    assert application.mark(postings[0].id, APPLIED)["ok"]

    payload = application.board_payload()
    statuses = {r["title"]: r["status"] for r in payload["roles"]}
    assert statuses["Senior Data Engineer"] == APPLIED
    assert payload["counts"]["applied"] == 1


def test_moving_the_weights_reorders_the_board_without_any_model_call(app):
    application, _ = app
    # All-in on likelihood: the older, lower-odds remote role must fall further.
    application.set_weights({"fit": 0, "likelihood": 100, "recency": 0})
    payload = application.board_payload()
    assert [r["title"] for r in payload["roles"]] == [
        "Senior Data Engineer", "Platform Engineer"]

    # All-in on fit only narrows the gap; flip to recency and the fresh one wins.
    application.set_weights({"fit": 0, "likelihood": 0, "recency": 100})
    payload = application.board_payload()
    assert payload["roles"][0]["title"] == "Senior Data Engineer"
    assert payload["roles"][0]["recency_score"] > payload["roles"][1]["recency_score"]


def test_weights_are_persisted_for_the_cli_to_pick_up(app):
    application, _ = app
    application.set_weights({"fit": 10, "likelihood": 80, "recency": 10, "halflife": 7})
    saved = json.loads((application.settings.data_dir / "weights.json").read_text())
    assert saved["likelihood"] == 80.0
    assert saved["halflife_days"] == 7.0


def test_the_page_is_self_contained(app):
    """No CDN, no external fetches — this page is looking at a job search."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<title>jobscout</title>" in html
    for remote in ("http://", "https://cdn", "src=\"//"):
        assert remote not in html.replace('target="_blank"', "")


def test_requirements_are_re_checked_on_every_read(app):
    """Moving a filter must show its effect at once, not on the next run."""
    application, _ = app

    payload = application.board_payload()
    assert all(r["requirement_ok"] for r in payload["roles"])

    # A floor above everything on the board, but keep postings that say nothing.
    application.set_requirements({"salary_min": 500000, "unknown_salary": "include"})
    payload = application.board_payload()
    assert all(r["requirement_ok"] for r in payload["roles"]), \
        "postings with no stated salary must survive an include policy"

    # Same floor, now dropping unknowns: everything goes, and says why.
    application.set_requirements({"unknown_salary": "exclude"})
    payload = application.board_payload()
    assert not any(r["requirement_ok"] for r in payload["roles"])
    assert all("drop unknowns" in r["requirement_reason"] for r in payload["roles"])


def test_requirements_survive_a_restart(app, tmp_path):
    application, _ = app
    application.set_requirements({"salary_min": 150000, "unknown_salary": "exclude",
                                 "exclude_title_words": "sales, intern"})

    from jobscout.config import load_requirements

    reloaded = load_requirements(application.settings.data_dir)
    assert reloaded.salary_min == 150000
    assert reloaded.unknown_salary == "exclude"
    assert reloaded.exclude_title_words == ["sales", "intern"]


# --- the app configures itself; nothing is assumed -------------------------

def test_the_app_starts_with_nothing_configured(tmp_path):
    """`jobscout serve` must not require a .env or any prior setup."""
    blank = Settings(applications_dir=tmp_path / "nope",
                     data_dir=tmp_path / "state", backend="mock")
    application = App(blank)
    assert not application.configured()
    assert not application.start_run()["started"]
    assert "choose the folder" in application.start_run()["reason"]


def test_the_folder_is_chosen_in_the_page_and_inspected_first(tmp_path):
    apps = tmp_path / "apps" / "Some Lab"
    apps.mkdir(parents=True)
    (apps / "resume.txt").write_text("Nine years of pipelines.", encoding="utf-8")

    application = App(Settings(applications_dir=tmp_path / "nope",
                               data_dir=tmp_path / "state", backend="mock"))

    listing = application.browse(str(tmp_path))
    assert "apps" in [e["name"] for e in listing["entries"]]

    # You are told what is in a folder before committing to it.
    look = application.inspect(str(tmp_path / "apps"))
    assert look["ok"] and look["documents"] == 1 and look["companies"] == ["Some Lab"]

    assert application.configure({"applications_dir": str(tmp_path / "apps")})["ok"]
    assert application.configured()


def test_a_folder_with_nothing_readable_is_reported_not_silently_accepted(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    application = App(Settings(applications_dir=tmp_path, data_dir=tmp_path / "s",
                               backend="mock"))
    look = application.inspect(str(empty))
    assert not look["ok"] and "no readable documents" in look["note"]


def test_location_is_set_from_the_page(tmp_path):
    application = App(Settings(applications_dir=tmp_path, data_dir=tmp_path / "s",
                               backend="mock"))
    application.configure({"location": {"states": "OR, WA", "cities": "Portland",
                                        "allow_remote": False, "allow_hybrid": True},
                           "max_age_days": 14})
    policy = application.settings.location
    assert policy.allowed_states == ["OR", "WA"]
    assert policy.allowed_cities == ["portland"]
    assert policy.allow_remote is False and policy.allow_hybrid is True
    assert application.settings.max_age_days == 14

    # And it takes effect on the board immediately, with no run.
    payload = application.board_payload()
    assert payload["location"]["states"] == ["OR", "WA"]


def test_the_saved_cache_is_opt_in(tmp_path):
    """Without it, a run gets its own folder — no inherited employers or history."""
    application = App(Settings(applications_dir=tmp_path, data_dir=tmp_path / "state",
                               backend="mock"))
    assert application.use_saved is False

    fresh = application.run_settings()
    assert fresh.data_dir != application.settings.data_dir
    assert "runs" in str(fresh.data_dir)

    application.configure({"use_saved": True})
    assert application.run_settings().data_dir == application.settings.data_dir


def test_settings_are_only_written_when_you_ask(tmp_path):
    application = App(Settings(applications_dir=tmp_path, data_dir=tmp_path / "state",
                               backend="mock"))
    application.configure({"location": {"states": "OR"}})
    assert not (tmp_path / "state" / "location.json").exists()

    application.configure({"location": {"states": "OR"}, "remember": True})
    assert (tmp_path / "state" / "location.json").exists()
