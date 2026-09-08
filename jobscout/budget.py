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
conservative *reservation* is checked before the call and reconciled with the
real figure after. Spending therefore stops at or below the cap, never above
it — at the price of refusing a call that might have fit.

The store is deliberately swappable. A single CLI run wants a file; several
worker pods sharing one budget want a row in Postgres they can update
atomically. The rule being enforced is the same either way.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Protocol

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
    """Where the running total lives."""

    def spent_on(self, day: str) -> float: ...

    def add(self, day: str, amount: float) -> float: ...

    def history(self) -> Dict[str, float]: ...


class FileSpendStore:
    """A JSON file next to the rest of jobscout's state.

    Correct for one process at a time, which is what the CLI is. Writes go
    through a temporary file and a rename so an interrupted write cannot leave
    a truncated ledger — losing the day's total would silently reset the cap.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

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
        return self._read().get(day, 0.0)

    def add(self, day: str, amount: float) -> float:
        data = self._read()
        total = data.get(day, 0.0) + amount
        data[day] = total
        self._write(data)
        return total

    def history(self) -> Dict[str, float]:
        return dict(sorted(self._read().items()))


class NullSpendStore:
    """Records nothing. Used when no cap is configured, and by the mock backend."""

    def spent_on(self, day: str) -> float:
        return 0.0

    def add(self, day: str, amount: float) -> float:
        return 0.0

    def history(self) -> Dict[str, float]:
        return {}


@dataclass
class Budget:
    """The cap, the ledger, and the two calls that enforce it."""

    cap_usd: float
    store: SpendStore
    reserve_usd: float = DEFAULT_RESERVE_USD

    @classmethod
    def from_settings(cls, settings) -> "Budget":
        cap = float(getattr(settings, "daily_budget_usd", 0.0) or 0.0)
        if cap <= 0:
            return cls(cap_usd=0.0, store=NullSpendStore())
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

    def check(self, reserve_usd: float | None = None) -> None:
        """Refuse a call that could take the day past its cap.

        Called before the request, not after, which is the only ordering that
        can actually prevent an overspend.
        """
        if not self.enabled:
            return
        reserve = self.reserve_usd if reserve_usd is None else reserve_usd
        spent = self.spent_today()
        if spent + reserve > self.cap_usd:
            raise BudgetExceeded(spent, self.cap_usd, reserve)

    def record(self, cost_usd: float) -> float:
        """Reconcile the reservation with what the call actually cost."""
        if not self.enabled or cost_usd <= 0:
            return self.spent_today()
        return self.store.add(self.today(), float(cost_usd))

    def summary(self) -> str:
        if not self.enabled:
            return "no daily budget set"
        return "$%.4f of $%.2f spent today ($%.2f left)" % (
            self.spent_today(), self.cap_usd, self.remaining())
