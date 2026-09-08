"""Politeness that several pods can agree on."""
from __future__ import annotations

import os

import pytest

from jobscout import ratelimit
from jobscout.ratelimit import LocalLimiter, NullLimiter, acquire, domain_of


@pytest.mark.parametrize("url,expected", [
    ("https://boards-api.greenhouse.io/v1/boards/acme", "greenhouse.io"),
    ("https://boards.greenhouse.io/acme", "greenhouse.io"),
    ("https://jobs.lever.co/acme", "lever.co"),
    ("https://careers.example.co.uk/jobs", "example.co.uk"),
    ("https://acme.com", "acme.com"),
    ("not a url", ""),
])
def test_buckets_are_keyed_by_operator_not_by_host(url, expected):
    """boards-api.greenhouse.io and boards.greenhouse.io are one operator's
    infrastructure. Two buckets would mean twice the agreed rate arriving at
    the same servers."""
    assert domain_of(url) == expected


def test_a_burst_is_allowed_and_then_the_rate_takes_over():
    limiter = LocalLimiter(rate=10.0, burst=3.0)
    assert [limiter.wait_for("acme.com") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert limiter.wait_for("acme.com") > 0


def test_one_domain_being_slow_does_not_hold_up_another():
    limiter = LocalLimiter(rate=1.0, burst=1.0)
    limiter.wait_for("busy.com")
    assert limiter.wait_for("busy.com") > 0
    assert limiter.wait_for("quiet.com") == 0.0


def test_acquire_gives_up_rather_than_pinning_a_worker():
    """One unresponsive domain must not block a worker that could be making
    progress on the other eleven."""
    slept = []
    limiter = LocalLimiter(rate=0.01, burst=1.0)
    assert acquire(limiter, "https://acme.com", timeout=30, sleep=slept.append)
    assert not acquire(limiter, "https://acme.com", timeout=5, sleep=slept.append)


def test_a_urlless_target_is_never_blocked():
    assert acquire(NullLimiter(), "")


def test_null_limiter_never_waits():
    assert NullLimiter().wait_for("anything") == 0.0


# --- the shared case, which is the only one that matters in a pool ----------

URL = os.environ.get("JOBSCOUT_TEST_REDIS", "redis://localhost:6399/0")


def _client():
    redis = pytest.importorskip("redis")
    try:
        client = redis.Redis.from_url(URL, decode_responses=True,
                                      socket_connect_timeout=1)
        client.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("no redis at %s" % URL)
    return client


def test_separate_clients_share_one_domains_allowance():
    """The property a local limiter cannot have. Two pods, one bucket: the
    second sees the first's spending, so the domain sees the agreed rate
    rather than the agreed rate times the number of pods."""
    from jobscout.ratelimit import RedisLimiter

    client = _client()
    prefix = "jobscout:test:rate"
    for key in client.scan_iter("%s:*" % prefix):
        client.delete(key)

    one = RedisLimiter(client, rate=1.0, burst=3.0, prefix=prefix)
    two = RedisLimiter(client, rate=1.0, burst=3.0, prefix=prefix)

    assert one.wait_for("acme.com") == 0.0
    assert two.wait_for("acme.com") == 0.0
    assert one.wait_for("acme.com") == 0.0
    # The burst of three is spent between them, not three each.
    assert two.wait_for("acme.com") > 0
