"""Being polite to other people's servers, from several pods at once.

Board discovery replaced paid model calls with HTTP requests, which is cheaper
for us and more work for whoever runs the board. One CLI probing Greenhouse a
few times a minute is nothing. Ten worker pods doing it without coordinating
is a small flood aimed at a handful of hosts -- and the failure mode is not a
bill, it is being blocked, which breaks the feature for everyone running
jobscout rather than just for us.

A token bucket per domain, held where every pod can see it. The interesting
part is that this cannot be solved locally: each pod limiting itself to two
requests a second still means twenty a second at the far end. The limit has to
be shared, which makes it the same shape as the spending budget -- read,
decide, and write in one atomic step -- and for the same reason.

Buckets are keyed by registrable domain, not by URL, because
``boards-api.greenhouse.io`` and ``boards.greenhouse.io`` are one operator's
infrastructure and should share one allowance.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional, Protocol
from urllib.parse import urlparse

#: Requests per second, per domain, by default. Deliberately gentle: nothing
#: here is latency-sensitive, and the whole point of probing is that it is
#: cheap for us -- there is no reason to make it expensive for them.
DEFAULT_RATE = 2.0
#: How many requests may arrive at once after a quiet period. A small burst is
#: what makes probing five ATS APIs for one company feel instant.
DEFAULT_BURST = 5.0
#: Give up waiting rather than block a worker forever behind one slow domain.
DEFAULT_TIMEOUT = 20.0


def domain_of(url: str) -> str:
    """The registrable domain, so subdomains share one allowance."""
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Two labels is right for .com and .io; three for the compound suffixes
    # that would otherwise collapse every site in the country into one bucket.
    if parts[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


class Limiter(Protocol):
    def wait_for(self, domain: str) -> float: ...


class LocalLimiter:
    """One process's own bucket. Correct for the CLI, wrong for a pool."""

    def __init__(self, rate: float = DEFAULT_RATE, burst: float = DEFAULT_BURST) -> None:
        self.rate, self.burst = rate, burst
        self._buckets: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def wait_for(self, domain: str) -> float:
        now = time.monotonic()
        with self._lock:
            tokens, ts = self._buckets.get(domain, (self.burst, now))
            tokens = min(self.burst, tokens + (now - ts) * self.rate)
            if tokens < 1.0:
                self._buckets[domain] = (tokens, now)
                return (1.0 - tokens) / self.rate
            self._buckets[domain] = (tokens - 1.0, now)
            return 0.0


class RedisLimiter:
    """One bucket per domain, shared by every pod.

    The script is the whole point: read the bucket, refill it for elapsed
    time, and either take a token or report how long to wait -- atomically, so
    two pods cannot both see the last token.
    """

    _TAKE = """
    local bucket = redis.call('HMGET', KEYS[1], 't', 'ts')
    local tokens = tonumber(bucket[1])
    local ts = tonumber(bucket[2])
    local rate = tonumber(ARGV[1])
    local burst = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    if tokens == nil or ts == nil then
      tokens = burst
      ts = now
    end
    tokens = math.min(burst, tokens + (now - ts) * rate)
    local wait = 0
    if tokens < 1 then
      wait = (1 - tokens) / rate
    else
      tokens = tokens - 1
    end
    redis.call('HMSET', KEYS[1], 't', tostring(tokens), 'ts', tostring(now))
    redis.call('EXPIRE', KEYS[1], ARGV[4])
    return tostring(wait)
    """

    TTL_SECONDS = 3600

    def __init__(self, client, rate: float = DEFAULT_RATE, burst: float = DEFAULT_BURST,
                 prefix: str = "jobscout:rate") -> None:
        self.client, self.rate, self.burst = client, rate, burst
        self.prefix = prefix
        self._take = client.register_script(self._TAKE)

    @classmethod
    def from_url(cls, url: str, **kw) -> "RedisLimiter":
        import redis

        return cls(redis.Redis.from_url(url, decode_responses=True), **kw)

    def wait_for(self, domain: str) -> float:
        return float(self._take(
            keys=["%s:%s" % (self.prefix, domain)],
            args=[repr(self.rate), repr(self.burst), repr(time.time()), self.TTL_SECONDS]))


class NullLimiter:
    """No limiting. For tests, and for anything already rate-limited elsewhere."""

    def wait_for(self, domain: str) -> float:
        return 0.0


def acquire(limiter: Limiter, url: str, timeout: float = DEFAULT_TIMEOUT,
            sleep=time.sleep) -> bool:
    """Block until this domain has an allowance, or give up.

    Giving up matters: one unresponsive domain must not pin a worker that
    could be making progress on the other eleven.
    """
    domain = domain_of(url)
    if not domain:
        return True
    deadline = time.monotonic() + timeout
    while True:
        wait = limiter.wait_for(domain)
        if wait <= 0:
            return True
        if time.monotonic() + wait > deadline:
            return False
        sleep(min(wait, 1.0))
