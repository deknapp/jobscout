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
