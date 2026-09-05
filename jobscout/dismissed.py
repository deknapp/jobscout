"""Leads you have killed, and the reason why.

A recommender that cannot be told "no" is only useful once. The second time it
runs it offers back the San Francisco role you already declined, the recruiter
who wasted three weeks of your time, and the contact you decided not to spend —
and every one of those costs you the small amount of attention that made you
open the list at all.

The rest of jobscout suppresses a *posting* through :mod:`history`, keyed on the
posting's id. That does not fit here, because the things worth killing in a
network are people and employers, and they arrive under different names in
different files: a LinkedIn URL in one, an email address in another, a company
spelled three ways across an inbox. So this keeps its own small store, keyed on
whichever identity is stable for that kind of thing.

Two rules it follows deliberately:

* **A dismissal always carries a reason.** Six weeks later "why did I rule this
  out?" is a real question, and a kill-list that cannot answer it gets ignored
  or, worse, cleared.
* **Nothing is hidden silently.** Dismissed items are filtered from the default
  view and counted in the footer, never deleted. Anything the tool withholds
  from someone trying to find a job must be visible and reversible.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .corpus import normalize_company

PERSON = "person"
EMPLOYER = "employer"
KINDS = (PERSON, EMPLOYER)


def person_key(identity: str) -> str:
    """A stable key for a human, from whatever you happen to have.

    A LinkedIn profile slug beats an address, which beats a name — a name is the
    weakest because two people share one and one person spells theirs three ways.
    """
    text = (identity or "").strip().lower()
    if not text:
        return ""
    match = re.search(r"/in/([^/?#]+)", text)
    if match:
        return "in:%s" % match.group(1)
    if "@" in text:
        return "mail:%s" % text
    return "name:%s" % re.sub(r"\s+", " ", text)


def employer_key(name: str) -> str:
    key = normalize_company(name)
    return "co:%s" % key if key else ""


@dataclass
class Dismissal:
    key: str = ""
    kind: str = PERSON
    label: str = ""
    reason: str = ""
    when: str = ""

    @property
    def summary(self) -> str:
        return "%s — %s (%s)" % (self.label or self.key, self.reason or "no reason given",
                                 self.when or "undated")


class Dismissals:
    """The kill-list, loaded from and saved to the data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: Dict[str, Dismissal] = {}
        self.load()

    def load(self) -> None:
        self.items = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        fields = set(Dismissal.__dataclass_fields__)  # type: ignore[attr-defined]
        for item in raw.get("dismissed", []):
            entry = Dismissal(**{k: v for k, v in item.items() if k in fields})
            if entry.key:
                self.items[entry.key] = entry

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated": dt.date.today().isoformat(),
                   "dismissed": [asdict(d) for d in self.sorted()]}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")

    def sorted(self) -> List[Dismissal]:
        return sorted(self.items.values(), key=lambda d: (d.kind, d.label.lower()))

    def add(self, kind: str, identity: str, reason: str, label: str = "",
            when: Optional[str] = None) -> Dismissal:
        key = employer_key(identity) if kind == EMPLOYER else person_key(identity)
        if not key:
            raise ValueError("nothing identifiable in %r" % identity)
        entry = Dismissal(key=key, kind=kind, label=label or identity, reason=reason,
                          when=when or dt.date.today().isoformat())
        self.items[key] = entry
        return entry

    def remove(self, identity: str) -> Optional[Dismissal]:
        for key in (person_key(identity), employer_key(identity)):
            if key in self.items:
                return self.items.pop(key)
        return None

    # --- lookups -----------------------------------------------------------
    def person(self, *identities: str) -> Optional[Dismissal]:
        """Match a human on any identity you have for them."""
        for identity in identities:
            key = person_key(identity or "")
            if key and key in self.items:
                return self.items[key]
        return None

    def employer(self, name: str) -> Optional[Dismissal]:
        key = employer_key(name or "")
        return self.items.get(key) if key else None

    def covers(self, *, person_ids: Iterable[str] = (), company: str = ""
               ) -> Optional[Dismissal]:
        """Is anything about this lead on the kill-list."""
        hit = self.person(*[p for p in person_ids if p])
        if hit:
            return hit
        return self.employer(company) if company else None

    def __len__(self) -> int:
        return len(self.items)
