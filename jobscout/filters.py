"""The hard filters.

Everything in this module is deterministic Python. The agents never get the last
word on whether a job is in the right place or recent enough — they propose, this
module disposes. That separation is the point: a model that decides Denver is
"basically New Mexico", or that a 2023 posting is "still probably open", gets
overruled here every time.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import LocationPolicy
from .models import Posting

US_STATES: Dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}

#: Phrases that mean "anywhere in the country", which satisfies a US-state policy.
NATIONWIDE = (
    "united states", "usa", "u.s.", "us-based", "us based", "nationwide",
    "anywhere in the us", "anywhere in the united states", "remote - us",
    "remote (us)", "remote, us", "us remote", "remote usa", "all 50 states",
    "anywhere in the country",
)

REMOTE_WORDS = ("remote", "work from home", "work-from-home", "wfh",
                "telework", "telecommute", "distributed", "virtual",
                "anywhere", "home-based", "home based")
HYBRID_WORDS = ("hybrid", "days in office", "days on-site", "days onsite",
                "days per week in", "partially remote", "flex office")

#: "Remote, but you must live in X" — the trap this filter exists to catch.
FENCE_PATTERNS = (
    re.compile(r"must (?:reside|live|be located|be based)(?:\s+\w+){0,3}\s+in\s+([^.;,()]{2,60})", re.I),
    re.compile(r"remote\s*[\-–(:]\s*([^.;)]{2,60})", re.I),
    re.compile(r"remote\s+(?:within|in|from|across)\s+([^.;,()]{2,60})", re.I),
    re.compile(r"open (?:only )?to candidates (?:in|located in|residing in)\s+([^.;,()]{2,60})", re.I),
    re.compile(r"eligible (?:to work )?(?:only )?in\s+([^.;,()]{2,60})", re.I),
    re.compile(r"\(([^)]{2,40})\s+(?:only|residents|based)\)", re.I),
)


def _word_in(text: str, needle: str) -> bool:
    return re.search(r"(?<![\w])%s(?![\w])" % re.escape(needle), text) is not None


def policy_places(policy: LocationPolicy) -> List[str]:
    """Every spelling of a place the policy accepts, lowercased."""
    places: List[str] = []
    for code in policy.allowed_states:
        places.append(code.lower())
        full = US_STATES.get(code.upper())
        if full:
            places.append(full)
    places.extend(policy.allowed_cities)
    places.extend(policy.remote_allowed_regions)
    return [p for p in places if p]


def mentions_allowed_place(text: str, policy: LocationPolicy) -> bool:
    lowered = text.lower()
    for place in policy_places(policy):
        if len(place) == 2:
            # A bare state code needs punctuation or a boundary around it, or
            # "in" and "or" would match half the postings on earth.
            if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(place), lowered):
                return True
        elif place in lowered:
            return True
    return False


#: A fence is a short phrase, so it is matched exactly rather than by substring.
FENCE_NATIONWIDE = {
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
    "us only", "usa only", "us-based", "us based", "anywhere", "anywhere in the us",
    "global", "worldwide", "north america", "continental us", "lower 48",
    "all 50 states", "nationwide", "remote us", "us remote",
}


def is_nationwide(text: str) -> bool:
    """Does this text mean "anywhere in the country" (or wider)?"""
    lowered = (text or "").strip().lower().strip(".,;:-–()[] ")
    if lowered in FENCE_NATIONWIDE:
        return True
    return any(phrase in lowered for phrase in NATIONWIDE)


def detect_work_mode(text: str) -> str:
    lowered = (text or "").lower()
    remote = any(word in lowered for word in REMOTE_WORDS)
    hybrid = any(word in lowered for word in HYBRID_WORDS)
    if hybrid:
        return "hybrid"
    if remote:
        return "remote"
    if lowered.strip():
        return "onsite"
    return "unknown"


def remote_fences(text: str) -> List[str]:
    """Region restrictions attached to a remote role, if any are stated."""
    found: List[str] = []
    for pattern in FENCE_PATTERNS:
        for match in pattern.finditer(text or ""):
            fence = match.group(1).strip().lower()
            if fence and fence not in found:
                found.append(fence)
    return found


def check_location(posting: Posting, policy: LocationPolicy) -> Tuple[bool, str, str]:
    """The hard location gate.

    Returns ``(accepted, work_mode, reason)``. The default is **reject**: a
    posting whose location cannot be established does not get the benefit of the
    doubt, because the cost of a false accept (an application you can't take) is
    much higher than a false reject (one of many jobs).
    """
    text = " ".join(part for part in (posting.location, posting.summary) if part).strip()
    if not text:
        return False, "unknown", "no location stated"

    mode = detect_work_mode(text)
    local = mentions_allowed_place(text, policy)

    # Somewhere we can physically get to: onsite and hybrid are both fine there.
    if local and mode in ("onsite", "hybrid", "unknown"):
        return True, mode, "in an accepted location"
    if local and mode == "remote":
        return True, "remote", "remote and tied to an accepted location"

    if mode == "hybrid" and not policy.allow_hybrid:
        return False, "hybrid", "hybrid, and the office is not in an accepted location"

    if mode == "remote":
        if not policy.allow_remote:
            return False, "remote", "remote roles are excluded by policy"
        fences = remote_fences(text)
        if not fences:
            if is_nationwide(text):
                return True, "remote", "remote, nationwide"
            return True, "remote", "remote with no stated region restriction"
        for fence in fences:
            if mentions_allowed_place(fence, policy) or is_nationwide(fence):
                return True, "remote", "remote, open to %s" % fence
        return False, "remote", "remote but restricted to %s" % fences[0]

    return False, mode, "location %r is outside the accepted area" % posting.location[:60]


def check_freshness(posting: Posting, max_age_days: int,
                    today: Optional[dt.date] = None) -> Tuple[bool, str]:
    """Drop stale postings. An undated posting survives only if verified live."""
    age = posting.age_days(today)
    if age is None:
        if posting.verified == "live":
            return True, "no post date, but the listing was fetched and is live"
        return False, "no post date and the listing could not be verified as live"
    if age < 0:
        return True, "posted today"
    if age > max_age_days:
        return False, "posted %d days ago (limit %d)" % (age, max_age_days)
    return True, "posted %d day(s) ago" % age


def check_verified(posting: Posting) -> Tuple[bool, str]:
    """Anything the verifier could not confirm is dropped.

    Models will happily invent a plausible job-board URL. Nothing reaches the
    report unless a second pass fetched the page and found the role on it.
    """
    if posting.verified == "live":
        return True, "verified live"
    if posting.verified == "dead":
        return False, "listing is closed or gone"
    if posting.verified == "mismatch":
        return False, "page does not match the claimed role: %s" % posting.verification_note[:120]
    return False, "not verified"


def dedupe(postings: Sequence[Posting]) -> List[Posting]:
    """Collapse the same role found by several searches."""
    seen_ids = set()
    seen_pairs = set()
    kept: List[Posting] = []
    for posting in postings:
        pair = (posting.company_key, posting.title_key)
        if posting.id in seen_ids:
            continue
        if pair[0] and pair[1] and pair in seen_pairs:
            continue
        seen_ids.add(posting.id)
        seen_pairs.add(pair)
        kept.append(posting)
    return kept


def excluded_company(posting: Posting, exclude: Iterable[str]) -> bool:
    keys = {re.sub(r"\s+", " ", e.strip().lower()) for e in exclude if e.strip()}
    if not keys:
        return False
    company = posting.company.lower()
    return any(key in company or key == posting.company_key for key in keys)
