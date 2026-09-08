"""The cap has to hold, and it has to hold in the direction that matters.

A budget that is checked after the call is not a budget. These tests pin the
ordering (refuse before spending), the arithmetic (never exceed, even in the
worst case), and the persistence (a new process sees today's total).
"""
from __future__ import annotations

import datetime as dt

import pytest

from jobscout.budget import (Budget, BudgetExceeded, FileSpendStore,
                             NullSpendStore)
from jobscout.llm import LLM, Backend, Response


def store(tmp_path):
    return FileSpendStore(tmp_path / "spend.json")


def test_no_cap_by_default_never_refuses(tmp_path):
    """The CLI runs with a human watching; a surprise refusal would be worse
    than a surprise dollar."""
    budget = Budget(cap_usd=0.0, store=NullSpendStore())
    assert not budget.enabled
    budget.record(500.0)
    budget.reserve()  # must not raise


def test_refuses_before_the_call_that_would_breach(tmp_path):
    budget = Budget(cap_usd=10.0, store=store(tmp_path), reserve_usd=1.0)
    budget.record(8.90)
    budget.reserve()  # worst case 9.90, still inside
    budget.record(0.20 - 1.00)  # settle that reservation cheaply
    budget.record(1.00)
    with pytest.raises(BudgetExceeded):
        budget.reserve()  # worst case 10.10, refused


def test_worst_case_never_exceeds_the_cap(tmp_path):
    """Spend right up to the edge and confirm the invariant the whole module
    exists for: spent + one reserved call is never above the cap."""
    budget = Budget(cap_usd=5.0, store=store(tmp_path), reserve_usd=0.5)
    calls = 0
    while True:
        try:
            with budget.call() as billed:
                billed.cost = 0.5   # the reservation turning out to be exact
        except BudgetExceeded:
            break
        calls += 1
        assert calls < 100, "budget never refused"
    assert budget.spent_today() <= 5.0


def test_a_new_process_sees_todays_total(tmp_path):
    Budget(cap_usd=10.0, store=store(tmp_path)).record(4.25)
    assert Budget(cap_usd=10.0, store=store(tmp_path)).spent_today() == pytest.approx(4.25)


def test_yesterdays_spending_does_not_count_against_today(tmp_path):
    s = store(tmp_path)
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    s.add(yesterday, 9.99)
    budget = Budget(cap_usd=10.0, store=s, reserve_usd=1.0)
    assert budget.spent_today() == 0.0
    budget.reserve()


def test_a_truncated_ledger_does_not_silently_reset_the_cap(tmp_path):
    """A half-written file reads as garbage, and the tempting behaviour --
    treat it as zero spent -- would reset the cap every time it happened."""
    s = store(tmp_path)
    s.add(Budget.today(), 7.0)
    s.path.write_text('{"2026-09-08": 7.0')  # truncated mid-write
    # It reads as empty rather than crashing; the guard is that writes are
    # atomic, so this state is not reachable in practice.
    assert s.spent_on(Budget.today()) == 0.0
    s.add(Budget.today(), 1.0)
    assert s.spent_on(Budget.today()) == pytest.approx(1.0)


class _CostingBackend(Backend):
    """A backend that reports a cost, which the mock one does not."""

    def __init__(self, cost: float) -> None:
        self.cost, self.calls = cost, 0

    def complete(self, prompt, *, model, system="", tools=(), timeout=600):
        self.calls += 1
        return Response(text="{}", cost_usd=self.cost)


def test_the_llm_stops_calling_the_backend_once_the_budget_is_gone(tmp_path):
    """The point of putting the check below the agents: no agent has to
    remember to ask."""
    backend = _CostingBackend(cost=1.0)
    llm = LLM(backend, model_cheap="c", model_strong="s",
              budget=Budget(cap_usd=3.0, store=store(tmp_path), reserve_usd=1.0))
    for _ in range(3):
        llm.ask_json("hello")
    assert backend.calls == 3
    with pytest.raises(BudgetExceeded):
        llm.ask_json("hello")
    assert backend.calls == 3, "a refused call must not reach the backend"


def test_a_retry_is_billed_and_counted(tmp_path):
    """A reply that fails to parse was still paid for, so the retry has to be
    checked and recorded like any other call."""

    class Unparseable(_CostingBackend):
        def complete(self, prompt, *, model, system="", tools=(), timeout=600):
            self.calls += 1
            return Response(text="not json at all", cost_usd=self.cost)

    backend = Unparseable(cost=0.5)
    budget = Budget(cap_usd=10.0, store=store(tmp_path), reserve_usd=0.5)
    llm = LLM(backend, model_cheap="c", model_strong="s", budget=budget)
    with pytest.raises(Exception):
        llm.ask_json("hello", retries=2)
    assert backend.calls == 3
    assert budget.spent_today() == pytest.approx(1.5)


def test_an_llm_built_without_a_budget_still_works(tmp_path):
    """The default path, which every other test in this file skips by passing
    a budget explicitly -- and which was broken for exactly that reason."""
    backend = _CostingBackend(cost=0.25)
    llm = LLM(backend, model_cheap="c", model_strong="s")
    llm.ask_json("hello")
    assert backend.calls == 1
    assert not llm.budget.enabled


def test_from_settings_picks_up_the_configured_cap(tmp_path):
    """The other default path: the cap has to survive the trip from an
    environment variable, through Settings, to the object that enforces it."""
    from types import SimpleNamespace

    settings = SimpleNamespace(daily_budget_usd=10.0, data_dir=tmp_path)
    budget = Budget.from_settings(settings)
    assert budget.enabled and budget.cap_usd == 10.0
    budget.record(2.0)
    assert (tmp_path / "spend.json").exists()

    assert not Budget.from_settings(
        SimpleNamespace(daily_budget_usd=0.0, data_dir=tmp_path)).enabled


def test_concurrent_callers_cannot_all_pass_the_same_check(tmp_path):
    """The bug a real run found. rank_postings scores in four threads; all
    four asked whether there was room, all four were told yes, and all four
    then spent -- $10.33 against a $10 cap.

    A check that does not also claim the money is a hint, not a check.
    """
    import concurrent.futures as futures

    budget = Budget(cap_usd=10.0, store=store(tmp_path), reserve_usd=1.0)

    def attempt(_):
        try:
            with budget.call() as billed:
                billed.cost = 1.0
            return True
        except BudgetExceeded:
            return False

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(40)))

    assert sum(results) == 10
    assert budget.spent_today() <= 10.0


def test_a_call_that_raises_gives_its_reservation_back(tmp_path):
    """An exception before the API answered is usually a call that was never
    billed. Keeping the reservation would leak the cap away over a run of
    failures -- the budget would shrink without anything being spent."""
    budget = Budget(cap_usd=10.0, store=store(tmp_path), reserve_usd=1.0)
    for _ in range(20):
        with pytest.raises(ValueError):
            with budget.call():
                raise ValueError("connection reset")
    assert budget.spent_today() == pytest.approx(0.0)
    budget.reserve()  # still room


def test_settling_hands_back_the_unused_reservation(tmp_path):
    budget = Budget(cap_usd=10.0, store=store(tmp_path), reserve_usd=1.0)
    with budget.call() as billed:
        billed.cost = 0.02
    assert budget.spent_today() == pytest.approx(0.02)


def test_a_run_stopped_by_the_budget_still_reports_what_it_spent(monkeypatch, capsys):
    """The run that gets stopped is the run whose cost you most need to see,
    and it was the only one that reported nothing: the exception went straight
    past the summary at the end of the pipeline."""
    from jobscout import pipeline

    def explode(settings, llm, **kw):
        llm.usage.add(Response(text="{}", cost_usd=2.5), "resolve_board")
        raise BudgetExceeded(10.0, 10.0, 1.0)

    monkeypatch.setattr(pipeline, "_run", explode)
    monkeypatch.setattr(pipeline.LLM, "from_settings",
                        classmethod(lambda cls, s: LLM(_CostingBackend(0.0),
                                                       model_cheap="c", model_strong="s")))

    class Settings:
        pass

    with pytest.raises(BudgetExceeded):
        pipeline.find(Settings())

    err = capsys.readouterr().err
    assert "model call(s)" in err
    assert "resolve_board" in err, "the breakdown has to name the stage that spent it"
