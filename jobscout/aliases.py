"""One employer, one name.

:func:`network.same_employer` already handles a rename it can see — "OpenEye
Scientific" and "OpenEye, Cadence Molecular Sciences" share a distinctive word,
so the tool works it out. What it cannot work out is a name change that leaves
no trace: SandboxAQ sends mail from ``sandboxaq.com`` and from
``sandboxquantum.com``, and OpenEye's people moved to ``cadence.com`` after the
acquisition. Nothing in the strings connects them.

There is no clever way to know that. Guessing harder would start merging real
companies, which is a worse failure than showing one twice — so this is simply
a file you can edit, and a command that writes it for you.

One thing is inferred, because the evidence is strong and local: when the same
person writes from two domains, those domains are usually one employer that
changed its name. That is exactly what an acquisition looks like from the
outside, and it is the case the file would otherwise have to be told about
manually every time.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .corpus import normalize_company


class Aliases:
    """Variant employer names, mapped to the one you want to see."""

    def __init__(self, path: Path) -> None:
        self.path = path
        #: normalised variant -> canonical display name
        self.map: Dict[str, str] = {}
        self.load()

    def load(self) -> None:
        self.map = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for canonical, variants in (raw.get("aliases") or {}).items():
            for variant in list(variants) + [canonical]:
                key = normalize_company(variant)
                if key:
                    self.map[key] = canonical

    def save(self) -> None:
        grouped: Dict[str, List[str]] = defaultdict(list)
        for key, canonical in sorted(self.map.items()):
            if key != normalize_company(canonical):
                grouped[canonical].append(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"updated": dt.date.today().isoformat(), "aliases": dict(grouped)},
            indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def add(self, canonical: str, *variants: str) -> None:
        for name in (canonical,) + variants:
            key = normalize_company(name)
            if key:
                self.map[key] = canonical

    def canonical(self, name: str) -> str:
        """The name to show, and to group on."""
        key = normalize_company(name)
        return self.map.get(key, name)

    def key(self, name: str) -> str:
        return normalize_company(self.canonical(name))

    def __len__(self) -> int:
        return len({v for v in self.map.values()})


def infer(pairs: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Find employers that are one employer, from people who span both.

    ``pairs`` is (person identity, employer) — normally an address's local part
    and the domain it came from. A person who writes to you from two domains is
    weak evidence on its own; a person whose *name* is identical across both is
    the ordinary shape of an acquisition, where everybody's address changes at
    once and nothing else does.

    Returns pairs of names that should be merged, for a human to confirm. It
    deliberately does not write anything: a wrong merge is silent and hard to
    notice later, so the decision stays with the person.
    """
    seen: Dict[str, set] = defaultdict(set)
    for person, employer in pairs:
        if person and employer:
            seen[person.strip().lower()].add(employer)
    merges: List[Tuple[str, str]] = []
    for employers in seen.values():
        names = sorted(employers)
        for index, first in enumerate(names):
            for second in names[index + 1:]:
                if normalize_company(first) == normalize_company(second):
                    continue
                pair = (first, second)
                if pair not in merges:
                    merges.append(pair)
    return merges
