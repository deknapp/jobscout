"""Filters that know the difference between "no" and "didn't say".

Most job filters have two outcomes, and quietly fold the third into the wrong
one. Ask for a $150k floor and every posting that simply does not print a salary
— which is most of them — disappears, without ever telling you that is what
happened. That is not filtering, it is losing things.

So every requirement here is tri-state:

    PASS      the posting meets it
    FAIL      the posting violates it
    UNKNOWN   the posting does not say

and each requirement carries its own ``on_unknown`` policy: keep it or drop it.
Independently, per filter, because the right answer differs. A missing salary is
usually worth a look — plenty of good employers do not publish one. A missing
location, when you cannot relocate, is not.

Whichever way a filter resolves an unknown, the report says so, so "nothing came
back" can always be traced to a filter you set rather than to the market.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

from .models import Posting

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

INCLUDE = "include"
EXCLUDE = "exclude"

#: Hours in a working year, for comparing an hourly rate against a salary floor.
HOURS_PER_YEAR = 2080

# --- salary ----------------------------------------------------------------

#: Only read a number as pay if it is money or sits next to a pay word. Without
#: this, "401k matching" becomes a $401,000 salary.
_PAY_CONTEXT = re.compile(
    r"salary|compensation|pay|base|annual|per year|/\s*yr|/\s*year|per hour|"
    r"/\s*hr|hourly|rate|range|usd|\$", re.I)
_HOURLY = re.compile(r"per hour|/\s*hr\b|hourly|an hour|/\s*hour", re.I)
_RETIREMENT = re.compile(r"401\s*\(?k\)?|403\s*\(?b\)?", re.I)

_AMOUNT = re.compile(
    r"""(?<![\w.])                      # not mid-number
        \$?\s*
        (?P<num>\d{1,3}(?:,\d{3})+      # 150,000
              | \d+(?:\.\d+)?           # 150 or 150.5
        )
        \s*(?P<suffix>[kK]\b)?
    """, re.X)


def parse_salary(*texts: str) -> Optional[Tuple[int, int]]:
    """Best-effort ``(low, high)`` annual figures from posting text.

    Returns ``None`` when the posting does not state pay — which is the common
    case, and the whole reason ``on_unknown`` exists.
    """
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    # Strip retirement-plan mentions before looking for numbers.
    blob = _RETIREMENT.sub(" ", blob)
    if not _PAY_CONTEXT.search(blob):
        return None

    hourly = bool(_HOURLY.search(blob))
    values: List[int] = []
    for match in _AMOUNT.finditer(blob):
        raw = match.group("num").replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if match.group("suffix"):
            value *= 1000
        # A bare small number next to a pay word is an hourly rate.
        if hourly and value < 1000:
            value *= HOURS_PER_YEAR
        elif value < 1000:
            continue  # a year count, a team size, a street number
        if value > 10_000_000:
            continue
        values.append(int(round(value)))

    if not values:
        return None
    return min(values), max(values)


# --- clearance and employment type ----------------------------------------

_CLEARANCE = re.compile(
    r"active\s+(?:security\s+)?clearance|"
    r"\b(?:ts/sci|top secret|secret clearance|q[\s-]clearance|l[\s-]clearance)\b|"
    r"must (?:possess|hold|have)\s+(?:an?\s+)?active|"
    r"current\s+(?:dod\s+)?(?:security\s+)?clearance",
    re.I)
_CLEARANCE_NOT_REQUIRED = re.compile(
    r"clearance (?:is )?(?:not required|preferred but not|eligible)|"
    r"ability to obtain (?:a |an )?clearance|clearance[- ]eligible", re.I)

EMPLOYMENT_PATTERNS = (
    ("internship", re.compile(r"\bintern(ship)?\b|\bco-?op\b", re.I)),
    ("contract", re.compile(r"\bcontract(or)?\b|\bcontract-to-hire\b|\btemporary\b|"
                            r"\bfixed[- ]term\b|\bc2c\b|\b1099\b", re.I)),
    ("part-time", re.compile(r"\bpart[- ]time\b|\bpart time\b", re.I)),
    ("full-time", re.compile(r"\bfull[- ]time\b|\bfull time\b|\bpermanent\b|"
                             r"\bregular\b|\bfte\b", re.I)),
)


def detect_employment_type(*texts: str) -> Optional[str]:
    blob = " ".join(t for t in texts if t)
    for name, pattern in EMPLOYMENT_PATTERNS:
        if pattern.search(blob):
            return name
    return None


def requires_clearance(*texts: str) -> Optional[bool]:
    blob = " ".join(t for t in texts if t)
    if not blob.strip():
        return None
    if _CLEARANCE_NOT_REQUIRED.search(blob):
        return False
    if _CLEARANCE.search(blob):
        return True
    return None


# --- the requirement set ---------------------------------------------------

@dataclass
class Requirements:
    """Every filter, each with its own policy for postings that do not say.

    ``on_unknown`` fields take ``"include"`` or ``"exclude"``. The defaults lean
    the way the cost of being wrong leans: a missing salary is usually worth a
    look, a missing location — when you cannot relocate — is not.
    """

    # salary
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    unknown_salary: str = INCLUDE

    # employment type: which are acceptable, e.g. ["full-time"]
    employment_types: List[str] = field(default_factory=list)
    unknown_employment: str = INCLUDE

    # security clearance
    exclude_clearance_required: bool = False
    unknown_clearance: str = INCLUDE

    # title screening
    exclude_title_words: List[str] = field(default_factory=list)
    require_title_words: List[str] = field(default_factory=list)

    # freshness. Lives here because it IS a filter, and because a setting that
    # only existed for one session silently reverted between runs.
    max_age_days: Optional[int] = None

    # the two the pipeline already enforced, now with the policy made explicit
    unknown_location: str = EXCLUDE
    unknown_date: str = EXCLUDE

    def normalized(self) -> "Requirements":
        def policy(value: str, default: str) -> str:
            value = (value or "").strip().lower()
            return value if value in (INCLUDE, EXCLUDE) else default

        return Requirements(
            salary_min=self.salary_min,
            salary_max=self.salary_max,
            max_age_days=self.max_age_days,
            unknown_salary=policy(self.unknown_salary, INCLUDE),
            employment_types=[t.strip().lower() for t in self.employment_types if t.strip()],
            unknown_employment=policy(self.unknown_employment, INCLUDE),
            exclude_clearance_required=bool(self.exclude_clearance_required),
            unknown_clearance=policy(self.unknown_clearance, INCLUDE),
            exclude_title_words=[w.strip().lower() for w in self.exclude_title_words if w.strip()],
            require_title_words=[w.strip().lower() for w in self.require_title_words if w.strip()],
            unknown_location=policy(self.unknown_location, EXCLUDE),
            unknown_date=policy(self.unknown_date, EXCLUDE),
        )

    # --- individual checks, each tri-state ---------------------------------
    def check_salary(self, posting: Posting) -> Tuple[str, str]:
        if self.salary_min is None and self.salary_max is None:
            return PASS, ""
        found = parse_salary(posting.salary, posting.summary)
        if found is None:
            return UNKNOWN, "no salary stated"
        low, high = found
        if self.salary_min is not None and high < self.salary_min:
            return FAIL, ("pays up to $%s, below your $%s floor"
                          % (f"{high:,}", f"{self.salary_min:,}"))
        if self.salary_max is not None and low > self.salary_max:
            return FAIL, ("starts at $%s, above your $%s ceiling"
                          % (f"{low:,}", f"{self.salary_max:,}"))
        return PASS, "pays $%s–$%s" % (f"{low:,}", f"{high:,}")

    def check_employment(self, posting: Posting) -> Tuple[str, str]:
        if not self.employment_types:
            return PASS, ""
        found = detect_employment_type(posting.title, posting.salary, posting.summary)
        if found is None:
            return UNKNOWN, "employment type not stated"
        if found in self.employment_types:
            return PASS, found
        return FAIL, "is %s" % found

    def check_clearance(self, posting: Posting) -> Tuple[str, str]:
        if not self.exclude_clearance_required:
            return PASS, ""
        found = requires_clearance(posting.title, posting.summary)
        if found is None:
            return UNKNOWN, "clearance requirement not stated"
        if found:
            return FAIL, "requires an active clearance"
        return PASS, "no active clearance required"

    def check_title(self, posting: Posting) -> Tuple[str, str]:
        title = (posting.title or "").lower()
        if not title:
            return UNKNOWN, "no title"
        for word in self.exclude_title_words:
            if word in title:
                return FAIL, "title contains %r" % word
        if self.require_title_words:
            if not any(word in title for word in self.require_title_words):
                return FAIL, ("title matches none of: %s"
                              % ", ".join(self.require_title_words))
        return PASS, ""

    # --- resolution --------------------------------------------------------
    def resolve(self, state: str, detail: str, on_unknown: str,
                label: str) -> Tuple[bool, str]:
        if state == PASS:
            return True, detail
        if state == FAIL:
            return False, detail
        if on_unknown == INCLUDE:
            return True, "%s unknown, kept by your setting" % label
        return False, "%s — excluded because you asked to drop unknowns" % detail

    def check(self, posting: Posting) -> Tuple[bool, str]:
        """Apply every requirement. Returns ``(accepted, reason)``."""
        checks = (
            (self.check_salary(posting), self.unknown_salary, "salary"),
            (self.check_employment(posting), self.unknown_employment, "employment type"),
            (self.check_clearance(posting), self.unknown_clearance, "clearance"),
            (self.check_title(posting), INCLUDE, "title"),
        )
        notes = []
        for (state, detail), on_unknown, label in checks:
            accepted, reason = self.resolve(state, detail, on_unknown, label)
            if not accepted:
                return False, reason
            if reason:
                notes.append(reason)
        return True, "; ".join(notes)

    def summary(self) -> str:
        parts = []
        if self.salary_min is not None or self.salary_max is not None:
            low = "$%s" % f"{self.salary_min:,}" if self.salary_min else "any"
            high = "$%s" % f"{self.salary_max:,}" if self.salary_max else "any"
            parts.append("pay %s–%s (no salary stated: %s)"
                         % (low, high, self.unknown_salary))
        if self.employment_types:
            parts.append("%s only (unstated: %s)"
                         % ("/".join(self.employment_types), self.unknown_employment))
        if self.exclude_clearance_required:
            parts.append("no active-clearance roles (unstated: %s)" % self.unknown_clearance)
        if self.exclude_title_words:
            parts.append("title excludes %s" % ", ".join(self.exclude_title_words))
        if self.require_title_words:
            parts.append("title must mention %s" % ", ".join(self.require_title_words))
        return "; ".join(parts) or "no extra requirements"

    def to_dict(self) -> Dict:
        return asdict(self)
