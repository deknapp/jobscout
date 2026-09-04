"""The board — every role jobscout has recommended, kept as a working list.

The history file records *decisions* (seen, recommended, applied, dismissed) and
stays small. The board keeps the full detail of the roles worth acting on, so the
web app has something to show between runs and you can work through a list rather
than re-reading yesterday's terminal output.

It lives in your data dir, outside the repo, like everything else personal.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .models import Posting


class Board:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: Dict[str, Dict] = {}
        self.load()

    def load(self) -> None:
        self.items = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in raw.get("roles", []):
            if item.get("id"):
                self.items[item["id"]] = item

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated": dt.date.today().isoformat(),
                   "roles": sorted(self.items.values(),
                                   key=lambda r: -(r.get("composite") or 0))}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")

    def merge(self, postings: Sequence[Posting],
              today: Optional[dt.date] = None) -> int:
        """Add new roles and refresh the scores of ones already on the board."""
        stamp = (today or dt.date.today()).isoformat()
        added = 0
        for posting in postings:
            record = posting.to_dict()
            existing = self.items.get(posting.id)
            if existing is None:
                record["first_seen"] = stamp
                added += 1
            else:
                record["first_seen"] = existing.get("first_seen", stamp)
            record["last_scored"] = stamp
            self.items[posting.id] = record
        return added

    def postings(self) -> List[Posting]:
        return [Posting.from_dict(item) for item in self.items.values()]

    def as_list(self) -> List[Dict]:
        return sorted(self.items.values(), key=lambda r: -(r.get("composite") or 0))
