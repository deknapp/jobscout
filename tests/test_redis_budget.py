"""The shared budget, against a real Redis.

Skipped unless one is reachable. These tests exist because the thing being
claimed -- that two pods cannot both spend the same dollar -- is a property of
Redis running the script atomically, and a fake client would only prove that
the fake behaves as written.

    docker run --rm -p 6399:6379 redis:7-alpine
    JOBSCOUT_TEST_REDIS=redis://localhost:6399/0 pytest tests/test_redis_budget.py
"""
from __future__ import annotations

import concurrent.futures as futures
import os

import pytest

from jobscout.budget import Budget, BudgetExceeded

URL = os.environ.get("JOBSCOUT_TEST_REDIS", "redis://localhost:6399/0")


def _store(prefix: str):
    redis = pytest.importorskip("redis")
    from jobscout.budget import RedisSpendStore

    try:
        client = redis.Redis.from_url(URL, decode_responses=True,
                                      socket_connect_timeout=1)
        client.ping()
    except Exception:  # noqa: BLE001 - any connection problem means skip
        pytest.skip("no redis at %s" % URL)
    store = RedisSpendStore(client, prefix=prefix)
    for key in client.scan_iter("%s:*" % prefix):
        client.delete(key)
    return store


def test_separate_clients_cannot_both_spend_the_same_dollar():
    """Eight clients, as eight pods would be -- each with its own connection,
    none of them aware of the others except through the ledger."""
    from jobscout.budget import RedisSpendStore

    prefix = "jobscout:test:shared"
    ledger = _store(prefix)

    def worker(_):
        budget = Budget(cap_usd=10.0, reserve_usd=1.0,
                        store=RedisSpendStore(ledger.client, prefix=prefix))
        got = 0
        for _ in range(20):
            try:
                with budget.call() as billed:
                    billed.cost = 1.0
                got += 1
            except BudgetExceeded:
                break
        return got

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))

    assert sum(results) == 10
    assert ledger.spent_on(Budget.today()) == pytest.approx(10.0)


def test_settling_hands_back_what_was_not_spent():
    store = _store("jobscout:test:settle")
    budget = Budget(cap_usd=10.0, store=store, reserve_usd=1.0)
    with budget.call() as billed:
        billed.cost = 0.02
    assert budget.spent_today() == pytest.approx(0.02)


def test_releases_cannot_drive_the_ledger_negative():
    """A run of calls that never reached the API releases its reservations. On
    a fresh key that would leave a credit, and a credit quietly raises the
    next day's real cap."""
    store = _store("jobscout:test:negative")
    budget = Budget(cap_usd=10.0, store=store, reserve_usd=1.0)
    for _ in range(5):
        with budget.call():
            pass
    assert budget.spent_today() == pytest.approx(0.0)


def test_the_cap_still_refuses_once_the_shared_ledger_is_full():
    store = _store("jobscout:test:refuse")
    budget = Budget(cap_usd=2.0, store=store, reserve_usd=1.0)
    with budget.call() as billed:
        billed.cost = 1.0
    with budget.call() as billed:
        billed.cost = 1.0
    with pytest.raises(BudgetExceeded):
        budget.reserve()
