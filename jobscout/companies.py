"""The employer registry — the list the tool builds up about *you*, over time.

Searching "jobs in <your city>" is a bad way to find a job. Deciding which
employers could plausibly want someone with your background, finding each one's
real careers board once, and then reading those boards directly is a much better
one — and it gets cheaper every run, because a company's Greenhouse URL is
resolved once and remembered forever.

That memory lives in ``<data_dir>/companies.json``, outside the repo. You can
hand-edit it: add employers the model missed, pin a careers URL it got wrong, or
set ``"status": "ignored"`` for somewhere you would never work.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .corpus import normalize_company
from .sources import clean_board_url

NEW = "new"            # proposed by the model, careers board not found yet
RESOLVED = "resolved"  # we know where its jobs live
NO_BOARD = "no_board"  # looked, found nothing usable
IGNORED = "ignored"    # you never want to see this employer


@dataclass
class Company:
    name: str
    why: str = ""
    careers_url: str = ""
    ats: str = ""
    presence: str = ""            # how they satisfy the location policy
    status: str = NEW
    added: str = ""
    last_resolved: str = ""
    last_scanned: str = ""
    postings_found: int = 0
    note: str = ""
    #: You have written an application to this employer. They are the strongest
    #: signal in the registry — you have already decided you want to work there
    #: — so they resolve and scan ahead of anything a model merely proposed.
    applied_to: bool = False

    @property
    def key(self) -> str:
        return normalize_company(self.name)

    def scanned_days_ago(self, today: Optional[dt.date] = None) -> Optional[int]:
        if not self.last_scanned:
            return None
        try:
            when = dt.date.fromisoformat(self.last_scanned[:10])
        except ValueError:
            return None
        return ((today or dt.date.today()) - when).days


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.companies: Dict[str, Company] = {}
        self.load()

    def load(self) -> None:
        self.companies = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in raw.get("companies", []):
            fields = set(Company.__dataclass_fields__)  # type: ignore[attr-defined]
            company = Company(**{k: v for k, v in item.items() if k in fields})
            # Heal a malformed board URL on the way in, not only when it is first
            # resolved: a bad one written by an older run (or edited by hand)
            # would otherwise sit in the file forever, silently 404ing.
            company.careers_url = clean_board_url(company.careers_url)
            if company.key:
                self.companies[company.key] = company

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated": dt.date.today().isoformat(),
            "companies": [asdict(c) for c in self.sorted()],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")

    def sorted(self) -> List[Company]:
        return sorted(self.companies.values(), key=lambda c: c.name.lower())

    def get(self, name: str) -> Optional[Company]:
        return self.companies.get(normalize_company(name))

    def add(self, company: Company) -> Company:
        """Insert, or fill gaps in an existing entry without clobbering it."""
        key = company.key
        if not key:
            return company
        existing = self.companies.get(key)
        if existing is None:
            company.added = company.added or dt.date.today().isoformat()
            self.companies[key] = company
            return company
        for attr in ("why", "careers_url", "ats", "presence", "note"):
            if not getattr(existing, attr) and getattr(company, attr):
                setattr(existing, attr, getattr(company, attr))
        if existing.status == NEW and company.status == RESOLVED:
            existing.status = RESOLVED
        # One-way: a model proposing an employer you already applied to must not
        # demote it back out of the priority queue.
        existing.applied_to = existing.applied_to or company.applied_to
        return existing

    def known_names(self) -> List[str]:
        return [c.name for c in self.sorted()]

    def active(self) -> List[Company]:
        return [c for c in self.sorted() if c.status != IGNORED]

    def needing_resolution(self) -> List[Company]:
        """Employers with no board yet, the ones you applied to first.

        Resolution is capped per run, so this order decides who gets looked at
        at all. An employer you have written an application to should never sit
        behind eighty model guesses in that queue.
        """
        pending = [c for c in self.active()
                   if c.status == NEW and not c.careers_url]
        return sorted(pending, key=lambda c: (not c.applied_to, c.name.lower()))

    def scannable(self, rescan_after_days: int = 3,
                  today: Optional[dt.date] = None) -> List[Company]:
        """Companies with a known board that we have not read recently.

        Employers you applied to go first, as in ``needing_resolution``. After
        those, the ones never read at all, then the ones read longest ago.

        The order matters more than it looks. Agent-driven scans are capped per
        run, so this decides which boards get READ and which are silently left
        for next time — and the tie-break used to be the company's NAME, which
        meant a search whose reach was settled by the alphabet. An employer
        starting with S waited behind every M and N, run after run, however
        good a match they were.
        """
        out = []
        for company in self.active():
            if not company.careers_url or company.status == NO_BOARD:
                continue
            age = company.scanned_days_ago(today)
            if age is None or age >= rescan_after_days:
                out.append(company)
        return sorted(out, key=lambda c: (
            not c.applied_to,
            # Never scanned sorts ahead of everything that has been.
            c.scanned_days_ago(today) is not None,
            -(c.scanned_days_ago(today) or 0),
            c.name.lower()))

    def mark_resolved(self, company: Company, careers_url: str, ats: str = "") -> None:
        company.careers_url = careers_url
        company.ats = ats
        company.status = RESOLVED if careers_url else NO_BOARD
        company.last_resolved = dt.date.today().isoformat()

    def mark_scanned(self, company: Company, found: int) -> None:
        company.last_scanned = dt.date.today().isoformat()
        company.postings_found = found

    def summary(self) -> str:
        by_status: Dict[str, int] = {}
        for company in self.companies.values():
            by_status[company.status] = by_status.get(company.status, 0) + 1
        parts = ["%d employer(s) known" % len(self.companies)]
        for status in (RESOLVED, NEW, NO_BOARD, IGNORED):
            if by_status.get(status):
                parts.append("%d %s" % (by_status[status], status))
        return ", ".join(parts)
