"""The history file — why you never see the same job twice.

An append-only JSONL log in your data dir (never in the repo). Every posting the
pipeline has ever *evaluated* is written down, not just the ones it recommended,
so a later run does not spend a web fetch re-checking a job it already rejected.

Rejections come in two flavours. Permanent ones (wrong state, too old, excluded
employer) suppress the posting forever. Transient ones — a page that would not
load, a verification that timed out — expire after a few days, because those say
something about the network, not about the job.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .models import Posting

RECOMMENDED = "recommended"
APPLIED = "applied"
DISMISSED = "dismissed"
DROPPED = "dropped"

#: How long a transient rejection keeps a posting out of the running.
#:
#: Deliberately short. A transient rejection says something went wrong on OUR
#: side — a page would not load, a verification timed out — not that the job is
#: unsuitable. Locking a good role out for a week because of our own failure is
#: a worse error than re-checking it tomorrow, and re-checking is cheap.
TRANSIENT_SUPPRESSION_DAYS = 2

#: Rejection reasons that will never change on their own.
PERMANENT_PREFIXES = (
    "location", "remote but restricted", "hybrid", "posted ",
    "no location stated", "excluded", "already applied", "listing is closed",
)


def is_permanent(reason: str) -> bool:
    lowered = (reason or "").lower()
    return any(lowered.startswith(prefix) for prefix in PERMANENT_PREFIXES)


@dataclass
class Entry:
    id: str
    company: str = ""
    title: str = ""
    url: str = ""
    location: str = ""
    posted: str = ""
    status: str = RECOMMENDED
    reason: str = ""
    #: Kept so later runs can percentile-rank against everything ever scored.
    fit_score: int = 0
    likelihood: int = 0
    composite: float = 0.0
    permanent: bool = True
    first_seen: str = ""
    last_seen: str = ""
    note: str = ""

    @classmethod
    def from_json(cls, data: Dict) -> "Entry":
        fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in fields})

    def suppresses(self, today: dt.date) -> bool:
        """Should this entry keep the posting out of a new run?"""
        if self.status in (RECOMMENDED, APPLIED, DISMISSED):
            return True
        if self.permanent:
            return True
        seen = _parse(self.last_seen) or _parse(self.first_seen)
        if seen is None:
            return False
        return (today - seen).days < TRANSIENT_SUPPRESSION_DAYS


def _parse(value: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


class History:
    """A JSONL log with in-memory indexes by id, URL and company+title."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: List[Entry] = []
        self._by_id: Dict[str, Entry] = {}
        self._by_url: Dict[str, Entry] = {}
        self._by_role: Dict[Tuple[str, str], Entry] = {}
        self.load()

    # --- persistence -------------------------------------------------------
    def load(self) -> None:
        self.entries = []
        self._by_id = {}
        self._by_url = {}
        self._by_role = {}
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = Entry.from_json(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue  # a half-written line should not break the tool
            self._index(entry)

    def _index(self, entry: Entry) -> None:
        existing = self._by_id.get(entry.id)
        if existing is not None:
            self.entries[self.entries.index(existing)] = entry
        else:
            self.entries.append(entry)
        self._by_id[entry.id] = entry
        posting = Posting(company=entry.company, title=entry.title, url=entry.url)
        if posting.url_key:
            self._by_url[posting.url_key] = entry
        if posting.company_key and posting.title_key:
            self._by_role[(posting.company_key, posting.title_key)] = entry

    def _append(self, entry: Entry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            for entry in self.entries:
                handle.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")

    # --- lookups -----------------------------------------------------------
    def find(self, posting: Posting) -> Optional[Entry]:
        entry = self._by_id.get(posting.id)
        if entry is not None:
            return entry
        if posting.url_key:
            entry = self._by_url.get(posting.url_key)
            if entry is not None:
                return entry
        if posting.company_key and posting.title_key:
            return self._by_role.get((posting.company_key, posting.title_key))
        return None

    def seen_before(self, posting: Posting,
                    today: Optional[dt.date] = None) -> Tuple[bool, str]:
        entry = self.find(posting)
        if entry is None:
            return False, ""
        today = today or dt.date.today()
        if not entry.suppresses(today):
            return False, ""
        if entry.status == APPLIED:
            return True, "you already applied to this (%s)" % (entry.last_seen or entry.first_seen)
        if entry.status == DISMISSED:
            return True, "you dismissed this on %s" % (entry.last_seen or entry.first_seen)
        if entry.status == RECOMMENDED:
            return True, "already recommended on %s" % (entry.first_seen or "a previous run")
        return True, "already ruled out: %s" % (entry.reason or "no reason recorded")

    # --- writes ------------------------------------------------------------
    def record(self, posting: Posting, status: str, reason: str = "",
               today: Optional[dt.date] = None) -> Entry:
        today = today or dt.date.today()
        stamp = today.isoformat()
        existing = self.find(posting)
        entry = Entry(
            id=posting.id,
            company=posting.company,
            title=posting.title,
            url=posting.url,
            location=posting.location,
            posted=posting.posted,
            status=status,
            reason=reason,
            fit_score=posting.fit_score,
            likelihood=posting.likelihood,
            composite=posting.composite,
            permanent=(status != DROPPED) or is_permanent(reason),
            first_seen=(existing.first_seen if existing else stamp) or stamp,
            last_seen=stamp,
            note=existing.note if existing else "",
        )
        self._index(entry)
        if existing is not None:
            self._rewrite()
        else:
            self._append(entry)
        return entry

    def mark(self, posting_id: str, status: str, note: str = "") -> Optional[Entry]:
        """Set the status of an already-recorded posting (`jobscout mark`)."""
        entry = self._by_id.get(posting_id)
        if entry is None:
            matches = [e for e in self.entries if e.id.startswith(posting_id)]
            if len(matches) != 1:
                return None
            entry = matches[0]
        entry.status = status
        entry.permanent = True
        entry.last_seen = dt.date.today().isoformat()
        if note:
            entry.note = note
        self._rewrite()
        return entry

    def forget_transient(self) -> int:
        """Drop every soft rejection, so they are reconsidered on the next run.

        For when a rejection reflects a bug that has since been fixed rather
        than anything about the job — `jobscout history --retry`.
        """
        keep = [e for e in self.entries
                if not (e.status == DROPPED and not e.permanent)]
        removed = len(self.entries) - len(keep)
        if removed:
            self.entries = keep
            self._by_id = {}
            self._by_url = {}
            self._by_role = {}
            entries, self.entries = keep, []
            for entry in entries:
                self._index(entry)
            self._rewrite()
        return removed

    def by_status(self, status: str) -> List[Entry]:
        return [e for e in self.entries if e.status == status]

    def scored_composites(self) -> List[float]:
        """Every composite score ever assigned — the percentile baseline.

        Only scored entries count. Postings dropped by a hard filter never
        reached the ranking agent, and folding their zeroes in would inflate
        every percentile.
        """
        return [e.composite for e in self.entries if e.composite > 0]
