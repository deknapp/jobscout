"""How a role gets ranked.

Three things decide whether a posting is worth your afternoon, and they are not
the same thing:

**fit** — how well your background matches what the role asks for.
**likelihood** — your realistic chance of actually getting it, which is a
    different question. A staff role at a famous company can be a perfect fit
    and still a lottery ticket; a lab role where you already hold the clearance
    and live in the right state is a worse fit on paper and a far better bet.
**recency** — a job posted three days ago is meaningfully more gettable than the
    same job posted three weeks ago. The pipeline has usually not seen the
    listing before it is a few days old, and by four weeks the shortlist is
    often already drawn.

Recency decays exponentially rather than linearly, because that is how a
requisition actually ages: fast at first, then it barely matters whether it has
been open 40 days or 60.

The composite is then reported as a **percentile** against every role jobscout
has ever scored for you. "88th percentile" answers the question you are actually
asking — *is this better than what usually crosses my desk?* — which a bare
score out of 100 does not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Posting

#: Below this many historical samples, a percentile is noise, so it is not shown.
MIN_PERCENTILE_SAMPLES = 8

#: A posting with no date at all, but verified live, sits at the median rather
#: than being rewarded or punished for the board's poor metadata.
UNDATED_RECENCY = 50.0


@dataclass
class Weights:
    fit: float = 0.45
    likelihood: float = 0.30
    recency: float = 0.25
    #: Days for the recency score to halve.
    halflife_days: float = 14.0

    def normalized(self) -> "Weights":
        total = self.fit + self.likelihood + self.recency
        if total <= 0:
            return Weights()
        return Weights(fit=self.fit / total, likelihood=self.likelihood / total,
                       recency=self.recency / total, halflife_days=self.halflife_days)

    def describe(self) -> str:
        norm = self.normalized()
        return ("fit %d%% · likelihood %d%% · recency %d%% (half-life %.0f days)"
                % (round(norm.fit * 100), round(norm.likelihood * 100),
                   round(norm.recency * 100), self.halflife_days))


def recency_score(age_days: Optional[int], halflife_days: float = 14.0) -> float:
    """100 for a posting made today, halving every ``halflife_days``."""
    if age_days is None:
        return UNDATED_RECENCY
    if halflife_days <= 0:
        return 100.0
    return 100.0 * (0.5 ** (max(0, age_days) / float(halflife_days)))


def composite(posting: Posting, weights: Weights,
              age_days: Optional[int] = None) -> float:
    norm = weights.normalized()
    recency = recency_score(
        posting.age_days() if age_days is None else age_days, weights.halflife_days)
    posting.recency_score = int(round(recency))
    score = (norm.fit * posting.fit_score
             + norm.likelihood * posting.likelihood
             + norm.recency * recency)
    return round(score, 2)


def percentile_of(value: float, baseline: Sequence[float]) -> Optional[int]:
    """Where ``value`` sits in ``baseline``, as a 0-100 percentile.

    Uses the midpoint convention, so a score equal to every other score lands at
    50 rather than 0 or 100.
    """
    samples = [s for s in baseline if s is not None]
    if len(samples) < MIN_PERCENTILE_SAMPLES:
        return None
    below = sum(1 for s in samples if s < value)
    equal = sum(1 for s in samples if s == value)
    return int(round(100.0 * (below + 0.5 * equal) / len(samples)))


def score_all(postings: Sequence[Posting], weights: Weights,
              baseline: Optional[Iterable[float]] = None,
              today=None) -> List[Posting]:
    """Score, percentile-rank and sort. Returns the same postings, best first."""
    for posting in postings:
        posting.composite = composite(
            posting, weights,
            age_days=posting.age_days(today) if today is not None else None)

    pool = [p.composite for p in postings]
    history = list(baseline or [])
    combined = history + pool
    for posting in postings:
        posting.percentile = percentile_of(posting.composite, combined)

    return sorted(postings, key=lambda p: (-p.composite, p.company.lower()))


def best_per_company(postings: Sequence[Posting], keep: int = 1
                     ) -> Tuple[List[Posting], List[Posting]]:
    """Keep each employer's best ``keep`` roles; hold the rest back.

    Returns ``(kept, held_back)``, both best-first.

    Nothing in the pipeline used to limit how much of a shortlist one employer
    could occupy, and the boards are not read on equal terms: a mid-size startup
    on Ashby hands over its entire board in one request, while a national lab
    hands over nothing at all. So a single readable employer supplied eleven of
    forty roles on a real board — not because it was eleven times the best
    match, but because it was legible.

    The point of the list is which EMPLOYERS are worth an afternoon. Once the
    best role at one of them is on it, the fourth-best is noise; it is recorded
    on the survivor as ``also_hiring`` so the count is still there to expand.
    """
    kept: List[Posting] = []
    held: List[Posting] = []
    best: Dict[str, Posting] = {}
    counts: Dict[str, int] = {}
    for posting in postings:            # already sorted best-first
        key = posting.company_key or posting.company.lower()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= max(1, keep):
            kept.append(posting)
            best.setdefault(key, posting)
        else:
            held.append(posting)
    for key, leader in best.items():
        leader.also_hiring = max(0, counts.get(key, 0) - max(1, keep))
    return kept, held


def band(percentile: Optional[int], composite_score: float) -> str:
    """A short human label, falling back to the raw score when history is thin."""
    if percentile is None:
        if composite_score >= 78:
            return "strong"
        if composite_score >= 62:
            return "worth a look"
        if composite_score >= 48:
            return "marginal"
        return "weak"
    if percentile >= 90:
        return "top 10% of everything seen"
    if percentile >= 75:
        return "top quarter"
    if percentile >= 50:
        return "above your median"
    return "below your median"
