"""Fit, likelihood and recency are three different questions."""
import datetime as dt

from jobscout.models import Posting
from jobscout.scoring import (MIN_PERCENTILE_SAMPLES, Weights, band,
                              best_per_company, composite, percentile_of,
                              recency_score, score_all)

TODAY = dt.date(2026, 9, 4)


def test_recency_halves_on_schedule():
    assert recency_score(0, 14) == 100.0
    assert round(recency_score(14, 14)) == 50
    assert round(recency_score(28, 14)) == 25


def test_recency_decays_monotonically():
    scores = [recency_score(days, 14) for days in range(0, 61, 5)]
    assert scores == sorted(scores, reverse=True)


def test_an_undated_posting_sits_at_the_median():
    assert recency_score(None) == 50.0


def test_weights_are_normalised_so_the_sliders_can_be_any_scale():
    weights = Weights(fit=90, likelihood=60, recency=50).normalized()
    assert abs(weights.fit + weights.likelihood + weights.recency - 1.0) < 1e-9


def test_a_high_fit_lottery_ticket_loses_to_a_realistic_bet():
    """The whole point of scoring likelihood separately."""
    weights = Weights(fit=0.45, likelihood=0.30, recency=0.25, halflife_days=14)
    lottery = Posting(company="Famous Co", title="Staff Engineer",
                      posted=(TODAY - dt.timedelta(days=3)).isoformat(),
                      fit_score=95, likelihood=12)
    realistic = Posting(company="Local Lab", title="Senior Engineer",
                        posted=(TODAY - dt.timedelta(days=3)).isoformat(),
                        fit_score=72, likelihood=78)
    ranked = score_all([lottery, realistic], weights, today=TODAY)
    assert ranked[0] is realistic


def test_a_fresh_posting_beats_an_identical_stale_one():
    weights = Weights()
    fresh = Posting(company="A", title="Engineer", fit_score=70, likelihood=70,
                    posted=(TODAY - dt.timedelta(days=1)).isoformat())
    stale = Posting(company="B", title="Engineer", fit_score=70, likelihood=70,
                    posted=(TODAY - dt.timedelta(days=28)).isoformat())
    ranked = score_all([stale, fresh], weights, today=TODAY)
    assert ranked[0] is fresh


def test_percentile_needs_enough_history_to_mean_anything():
    assert percentile_of(50, [1, 2, 3]) is None
    baseline = list(range(MIN_PERCENTILE_SAMPLES + 2))
    assert percentile_of(1000, baseline) == 100
    assert percentile_of(-1, baseline) == 0


def test_percentile_uses_the_midpoint_convention():
    # Every sample identical: the value belongs in the middle, not at an extreme.
    assert percentile_of(5, [5] * 20) == 50


def test_percentile_is_measured_against_history_not_just_this_run():
    weights = Weights()
    postings = [Posting(company="A", title="Engineer", fit_score=50, likelihood=50,
                        posted=TODAY.isoformat())]
    strong_history = [95.0] * 20
    score_all(postings, weights, baseline=strong_history, today=TODAY)
    assert postings[0].percentile is not None
    assert postings[0].percentile < 25  # mediocre next to what it usually sees


def test_band_falls_back_to_the_raw_score_without_history():
    assert band(None, 85) == "strong"
    assert band(95, 40) == "top 10% of everything seen"


# --- one employer must not own the shortlist --------------------------------

def _scored(company, title, composite):
    posting = Posting(company=company, title=title)
    posting.composite = composite
    return posting


def test_the_best_role_at_each_employer_is_the_one_offered():
    board = [_scored("Benchling", "Data Engineer", 90),
             _scored("Benchling", "Technical Solution Architect", 88),
             _scored("Benchling", "Solutions Delivery Manager", 70),
             _scored("Booz Allen Hamilton", "Software Developer, Senior", 80)]
    kept, held = best_per_company(board, keep=1)

    assert [p.title for p in kept] == ["Data Engineer", "Software Developer, Senior"]
    assert len(held) == 2
    assert kept[0].also_hiring == 2
    assert kept[1].also_hiring == 0


def test_a_legible_board_no_longer_supplies_a_quarter_of_the_list():
    """The real shape: 11 of 40 roles came from one employer on one ATS."""
    board = ([_scored("Benchling", "Role %d" % i, 90 - i) for i in range(11)]
             + [_scored("Employer %d" % i, "Role", 60 - i) for i in range(29)])
    kept, held = best_per_company(board, keep=1)

    assert sum(1 for p in kept if p.company == "Benchling") == 1
    assert len(kept) == 30 and len(held) == 10


def test_holding_roles_back_never_loses_them():
    board = [_scored("A", "one", 90), _scored("A", "two", 80),
             _scored("B", "three", 70)]
    kept, held = best_per_company(board, keep=1)
    assert len(kept) + len(held) == len(board)


def test_a_higher_cap_lets_more_of_one_employer_through():
    board = [_scored("A", "one", 90), _scored("A", "two", 80),
             _scored("A", "three", 70)]
    kept, held = best_per_company(board, keep=2)
    assert [p.title for p in kept] == ["one", "two"]
    assert kept[0].also_hiring == 1 and len(held) == 1


def test_a_cap_below_one_still_keeps_the_best_role():
    """A nonsense setting must not silently empty the board."""
    kept, held = best_per_company([_scored("A", "one", 90)], keep=0)
    assert len(kept) == 1 and not held
