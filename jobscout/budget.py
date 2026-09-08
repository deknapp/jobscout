"""A spending cap that is enforced, not intended.

Every model call jobscout makes costs money, and the expensive ones are the
agentic stages: a single strong-model call with web search can run to a
dollar. That is fine when you type ``jobscout find`` and watch it. It is not
fine when the same pipeline runs unattended on a schedule, where a loop that
retries, or a board that returns three thousand postings, turns a cheap day
into an expensive one while nobody is looking.

So the cap lives below the agents, at the one seam every call passes through,
and it refuses rather than warns.

**A cap cannot be exact, and pretending otherwise would be the bug.** The cost
of a call is only known after the API reports its token counts, so a check
that waited for the true number would always be one call too late. Instead a
conservative *reservation* is written to the ledger before the call and
reconciled with the real figure after. Spending therefore stops at or below
the cap, never above it — at the price of refusing a call that might have fit.

**The reservation and the check must be one atomic step**, which the first
version of this module got wrong and a real run proved: ``rank_postings``
scores in four threads, all four asked "is there room?", all four were told
yes, and all four then spent. $10.33 against a $10 cap. A check that does not
also claim the money is not a check, it is a hint. So the ledger is read,
tested and written under a single lock, and the reservation is released or
settled afterwards.

The store is deliberately swappable. A single CLI run wants a file; several
worker pods sharing one budget want a row in Postgres they can update
atomically. The rule being enforced is the same either way.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, Optional, Protocol

#: What a single call is assumed to cost before it is made. A strong-model
#: call with web search is the expensive case: eight searches at $0.01 plus
#: the fetched pages as input tokens. Set high enough that the reservation is
#: rarely an underestimate, because an underestimate is how a cap is exceeded.
DEFAULT_RESERVE_USD = 1.00

#: Days of history kept. Enough to answer "what did last week cost" without
#: the file growing forever.
KEEP_DAYS = 60


class BudgetExceeded(RuntimeError):
    """Raised instead of making a call that would breach the cap."""

    def __init__(self, spent: float, cap: float, reserve: float) -> None:
        self.spent, self.cap, self.reserve = spent, cap, reserve
        super().__init__(
            "daily budget reached: $%.2f of $%.2f spent today, and the next "
            "call is reserved at $%.2f. Raise JOBSCOUT_DAILY_BUDGET_USD, or "
            "wait until tomorrow." % (spent, cap, reserve)
        )


class SpendStore(Protocol):
    """Where the running total lives.

    ``reserve`` is the important one: it must test the cap and claim the money
    in a single atomic step, or concurrent callers will all pass the test
    before any of them claims anything.
    """

    def spent_on(self, day: str) -> float: ...

    def reserve(self, day: str, amount: float, cap: float) -> bool: ...

    def adjust(self, day: str, delta: float) -> float: ...

    def add(self, day: str, amount: float) -> float: ...

    def history(self) -> Dict[str, float]: ...


class FileSpendStore:
    """A JSON file next to the rest of jobscout's state.

    Safe across the threads jobscout actually uses and across separate
    processes on one machine, because every read-modify-write happens while
    holding an exclusive lock on a sibling lockfile. Writes go through a
    temporary file and a rename, so an interrupted write cannot leave a
    truncated ledger — losing the day's total would silently reset the cap.

    Not safe across machines. Worker pods sharing one budget need the same
    logic in a database that can do it in one statement; that is why the
    interface is a Protocol and not this class.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> Dict[str, float]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}

    def _write(self, data: Dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        pruned = {k: v for k, v in data.items() if k >= cutoff}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".spend-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(pruned, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def spent_on(self, day: str) -> float:
        with self._locked():
            return self._read().get(day, 0.0)

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        """Claim ``amount`` against the cap, or refuse. One atomic step."""
        with self._locked():
            data = self._read()
            spent = data.get(day, 0.0)
            if spent + amount > cap:
                return False
            data[day] = spent + amount
            self._write(data)
            return True

    def adjust(self, day: str, delta: float) -> float:
        with self._locked():
            data = self._read()
            total = max(0.0, data.get(day, 0.0) + delta)
            data[day] = total
            self._write(data)
            return total

    def add(self, day: str, amount: float) -> float:
        return self.adjust(day, amount)

    def history(self) -> Dict[str, float]:
        with self._locked():
            return dict(sorted(self._read().items()))


class NullSpendStore:
    """Records nothing, refuses nothing. Used when no cap is configured."""

    def spent_on(self, day: str) -> float:
        return 0.0

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        return True

    def adjust(self, day: str, delta: float) -> float:
        return 0.0

    def add(self, day: str, amount: float) -> float:
        return 0.0

    def history(self) -> Dict[str, float]:
        return {}


@dataclass
class Reservation:
    """Money claimed for one call, not yet reconciled."""

    budget: "Budget"
    amount: float
    day: str
    #: What the call actually cost. Left at zero if it never reached the API,
    #: which releases the whole reservation.
    cost: float = 0.0
    settled: bool = False


@dataclass
class Budget:
    """The cap, the ledger, and the two calls that enforce it."""

    cap_usd: float
    store: SpendStore
    reserve_usd: float = DEFAULT_RESERVE_USD

    @classmethod
    def from_settings(cls, settings) -> "Budget":
        """Pick a store from the environment.

        A file when one process is spending, Redis when several are. The rule
        being enforced is identical; only the place it is enforced moves.
        """
        cap = float(getattr(settings, "daily_budget_usd", 0.0) or 0.0)
        if cap <= 0:
            return cls(cap_usd=0.0, store=NullSpendStore())
        url = os.environ.get("JOBSCOUT_REDIS_URL", "")
        if url:
            return cls(cap_usd=cap, store=RedisSpendStore.from_url(url))
        return cls(cap_usd=cap, store=FileSpendStore(Path(settings.data_dir) / "spend.json"))

    @property
    def enabled(self) -> bool:
        return self.cap_usd > 0

    @staticmethod
    def today() -> str:
        return dt.date.today().isoformat()

    def spent_today(self) -> float:
        return self.store.spent_on(self.today())

    def remaining(self) -> float:
        return max(0.0, self.cap_usd - self.spent_today()) if self.enabled else float("inf")

    def reserve(self, reserve_usd: float | None = None) -> "Reservation":
        """Claim room for one call, or refuse.

        The claim is written to the ledger immediately, before the call is
        made. That is what makes it safe when several threads or pods ask at
        once: the money is gone the moment it is promised, so the second
        caller sees the first caller's claim rather than a stale total.
        """
        amount = self.reserve_usd if reserve_usd is None else reserve_usd
        if not self.enabled:
            return Reservation(budget=self, amount=0.0, day=self.today())
        day = self.today()
        if not self.store.reserve(day, amount, self.cap_usd):
            raise BudgetExceeded(self.store.spent_on(day), self.cap_usd, amount)
        return Reservation(budget=self, amount=amount, day=day)

    def settle(self, reservation: "Reservation", actual_usd: float) -> float:
        """Replace a reservation with what the call really cost.

        The difference can be negative -- reservations are deliberate
        overestimates -- so this usually hands money back.
        """
        if not self.enabled or reservation.settled:
            return self.spent_today()
        reservation.settled = True
        return self.store.adjust(reservation.day, float(actual_usd) - reservation.amount)

    @contextlib.contextmanager
    def call(self, reserve_usd: float | None = None) -> Iterator["Reservation"]:
        """Wrap one billed call. Set ``.cost`` on the way out.

        If the call raises, the reservation is released rather than kept --
        an exception before the API responded is usually a call that was
        never billed, and holding the reservation would leak the cap away
        over a run of failures.
        """
        reservation = self.reserve(reserve_usd)
        try:
            yield reservation
        finally:
            self.settle(reservation, reservation.cost)

    def record(self, cost_usd: float) -> float:
        """Add a known cost with no reservation. For costs learned after the
        fact, outside the call path."""
        if not self.enabled or cost_usd <= 0:
            return self.spent_today()
        return self.store.add(self.today(), float(cost_usd))

    def summary(self) -> str:
        if not self.enabled:
            return "no daily budget set"
        return "$%.4f of $%.2f spent today ($%.2f left)" % (
            self.spent_today(), self.cap_usd, self.remaining())


class RedisSpendStore:
    """The same ledger, shared by every pod.

    ``FileSpendStore`` is correct for one machine and useless for several: two
    workers on different nodes would each hold their own file and each believe
    the whole day's budget was theirs. The rule has to be enforced somewhere
    both of them can see.

    ``reserve`` is a Lua script rather than a read followed by a write, for
    exactly the reason the file store needs a lock. Redis runs a script
    atomically, so no two callers can both observe the same remaining budget
    and both claim it -- which is the bug that let a four-thread run spend
    $10.33 against a $10 cap.
    """

    #: Read the total, refuse if this claim would exceed the cap, otherwise
    #: claim it. One step, no window in between.
    _RESERVE = """
    local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
    local amount = tonumber(ARGV[1])
    local cap = tonumber(ARGV[2])
    if spent + amount > cap then
      return 0
    end
    redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
    redis.call('EXPIRE', KEYS[1], ARGV[3])
    return 1
    """

    #: Settling can hand money back, and must not drive the total negative --
    #: a release of an unspent reservation on a fresh key would otherwise
    #: leave a credit that quietly raises tomorrow's cap.
    _ADJUST = """
    local total = tonumber(redis.call('INCRBYFLOAT', KEYS[1], ARGV[1]))
    if total < 0 then
      redis.call('SET', KEYS[1], '0')
      total = 0
    end
    redis.call('EXPIRE', KEYS[1], ARGV[2])
    return tostring(total)
    """

    #: Long enough to answer "what did last week cost", short enough that the
    #: keys do not accumulate forever.
    TTL_SECONDS = 90 * 24 * 3600

    def __init__(self, client, prefix: str = "jobscout:spend") -> None:
        self.client = client
        self.prefix = prefix
        self._reserve = client.register_script(self._RESERVE)
        self._adjust = client.register_script(self._ADJUST)

    def _key(self, day: str) -> str:
        return "%s:%s" % (self.prefix, day)

    @classmethod
    def from_url(cls, url: str, prefix: str = "jobscout:spend") -> "RedisSpendStore":
        import redis  # imported lazily: the CLI does not need it

        return cls(redis.Redis.from_url(url, decode_responses=True), prefix)

    def spent_on(self, day: str) -> float:
        return float(self.client.get(self._key(day)) or 0.0)

    def reserve(self, day: str, amount: float, cap: float) -> bool:
        return bool(self._reserve(keys=[self._key(day)],
                                  args=[repr(float(amount)), repr(float(cap)),
                                        self.TTL_SECONDS]))

    def adjust(self, day: str, delta: float) -> float:
        return float(self._adjust(keys=[self._key(day)],
                                  args=[repr(float(delta)), self.TTL_SECONDS]))

    def add(self, day: str, amount: float) -> float:
        return self.adjust(day, amount)

    def history(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for key in self.client.scan_iter("%s:*" % self.prefix):
            day = key.rsplit(":", 1)[-1]
            out[day] = float(self.client.get(key) or 0.0)
        return dict(sorted(out.items()))
