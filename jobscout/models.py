"""The one data structure the whole pipeline passes around."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .corpus import normalize_company

_TITLE_NOISE = re.compile(r"\b(senior|sr|staff|principal|lead|ii|iii|iv|i|1|2|3|4|"
                          r"level|l\d|remote|us|usa|contract|full[\s-]?time)\b", re.I)


def normalize_title(title: str) -> str:
    text = re.sub(r"[^\w\s]", " ", title or "")
    text = _TITLE_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def canonical_url(url: str) -> str:
    """Drop tracking query strings so the same posting is one posting."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return "%s%s" % (host, path)


def parse_date(value: Any) -> Optional[dt.date]:
    """Accept the handful of date shapes models actually emit."""
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


@dataclass
class Posting:
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    source: str = ""
    posted: str = ""
    salary: str = ""
    summary: str = ""
    #: How far through the pipeline this posting has got. The web app shows
    #: roles the moment they are FOUND, then fills in the rest as it arrives,
    #: so a long run is readable from the first employer rather than the last.
    #:   found -> passed the free filters, not yet checked or scored
    #:   verified -> confirmed to be a real, open listing
    #:   scored -> ranked against your background
    stage: str = "found"
    #: Filled in by later stages.
    work_mode: str = ""
    verified: str = "unchecked"   # live | dead | mismatch | unchecked
    verification_note: str = ""
    #: How well your background matches the role, 0-100.
    fit_score: int = 0
    #: Your realistic chance of actually getting it, 0-100 — a different question.
    likelihood: int = 0
    #: Freshness, 0-100, decaying exponentially with the posting's age.
    recency_score: int = 0
    #: The weighted blend of the three, and where it sits against everything
    #: jobscout has ever scored for you.
    composite: float = 0.0
    percentile: Optional[int] = None
    fit_rationale: str = ""
    likelihood_rationale: str = ""
    resembles: str = ""           # which of your past applications it looks like
    concerns: str = ""
    #: Why a dropped posting was dropped (reporting only).
    rejected_reason: str = ""

    @property
    def company_key(self) -> str:
        return normalize_company(self.company)

    @property
    def title_key(self) -> str:
        return normalize_title(self.title)

    @property
    def url_key(self) -> str:
        return canonical_url(self.url)

    @property
    def posted_date(self) -> Optional[dt.date]:
        return parse_date(self.posted)

    def age_days(self, today: Optional[dt.date] = None) -> Optional[int]:
        posted = self.posted_date
        if posted is None:
            return None
        return ((today or dt.date.today()) - posted).days

    @property
    def id(self) -> str:
        """Stable identity: the URL if we have one, else company + title."""
        basis = self.url_key or ("%s|%s" % (self.company_key, self.title_key))
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["id"] = self.id
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Posting":
        spec = cls.__dataclass_fields__  # type: ignore[attr-defined]
        kwargs = {}
        for key, value in data.items():
            if key not in spec:
                continue
            # Only string fields get the None -> "" treatment; percentile is
            # legitimately None when there is not enough history to rank against.
            if value is None and spec[key].default == "":
                value = ""
            kwargs[key] = value
        return cls(**kwargs)
