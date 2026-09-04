"""Rendering a run as Markdown.

The report is written to your data dir and printed to stdout. It always shows
what was *filtered out* and why, because a job tool that only shows you its hits
gives you no way to tell "there is nothing out there this week" apart from "my
location filter is too tight".
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import Posting
from .scoring import band

MODE_LABEL = {
    "remote": "remote",
    "onsite": "onsite",
    "hybrid": "hybrid",
    "unknown": "location unclear",
}


def _age_phrase(posting: Posting, today: dt.date) -> str:
    age = posting.age_days(today)
    if age is None:
        return "no date shown"
    if age <= 0:
        return "posted today"
    if age == 1:
        return "posted yesterday"
    return "posted %d days ago" % age


def _headline_score(posting: Posting) -> str:
    """Percentile first when there is enough history for it to mean something."""
    label = band(posting.percentile, posting.composite)
    if posting.percentile is not None:
        return "`%d%s percentile · %s`" % (posting.percentile,
                                           _ordinal(posting.percentile), label)
    return "`score %.0f/100 · %s`" % (posting.composite, label)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def render(recommended: Sequence[Posting], dropped: Sequence[Posting],
           stats: Dict[str, int], *, usage: str = "", errors: Sequence[str] = (),
           deferred: Sequence[Posting] = (), today: Optional[dt.date] = None,
           location_summary: str = "", weights_summary: str = "",
           requirements_summary: str = "") -> str:
    today = today or dt.date.today()
    out: List[str] = []
    out.append("# Job scout — %s" % today.isoformat())
    out.append("")
    if location_summary:
        out.append("Location filter: **%s**" % location_summary)
        out.append("")
    if weights_summary:
        out.append("Ranked by: **%s**" % weights_summary)
        out.append("")
    if requirements_summary and requirements_summary != "no extra requirements":
        out.append("Requirements: **%s**" % requirements_summary)
        out.append("")

    if recommended:
        out.append("**%d role(s) worth your time**, from %d employer(s) tracked. "
                   "Every one was fetched and confirmed open."
                   % (len(recommended), stats.get("employers_known", 0)))
    else:
        out.append("**Nothing new cleared the filters this run.** "
                   "The breakdown below shows where things fell out — if it is all "
                   "*location*, the market is the problem; if it is all *already "
                   "seen*, you are simply caught up.")
    out.append("")

    for index, posting in enumerate(recommended, start=1):
        out.append("---")
        out.append("")
        out.append("## %d. %s — %s" % (index, posting.company, posting.title))
        out.append("")
        out.append(_headline_score(posting))
        out.append("")
        out.append("| | |")
        out.append("|---|---|")
        out.append("| **Scores** | fit %d · likelihood %d · recency %d → **%.0f** |"
                   % (posting.fit_score, posting.likelihood, posting.recency_score,
                      posting.composite))
        out.append("| **Location** | %s — %s |"
                   % (posting.location or "—", MODE_LABEL.get(posting.work_mode, posting.work_mode)))
        out.append("| **Posted** | %s%s |"
                   % (posting.posted or "not shown",
                      " (%s)" % _age_phrase(posting, today) if posting.posted else ""))
        if posting.salary:
            out.append("| **Salary** | %s |" % posting.salary)
        out.append("| **Source** | %s, verified live |" % (posting.source or "employer board"))
        out.append("| **Apply** | %s |" % posting.url)
        out.append("| **id** | `%s` |" % posting.id)
        out.append("")
        if posting.summary:
            out.append(posting.summary)
            out.append("")
        if posting.fit_rationale:
            out.append("**Why you:** %s" % posting.fit_rationale)
            out.append("")
        if posting.likelihood_rationale:
            out.append("**Your odds:** %s" % posting.likelihood_rationale)
            out.append("")
        if posting.resembles:
            out.append("**Closest to:** %s" % posting.resembles)
            out.append("")
        if posting.concerns:
            out.append("**Worth weighing:** %s" % posting.concerns)
            out.append("")

    if deferred:
        out.append("---")
        out.append("")
        out.append("## Held over for the next run (%d)" % len(deferred))
        out.append("")
        out.append("Verified and in-location, but past this run's report or "
                   "verification budget. They are deliberately **not** written to "
                   "the history, so the next run can still surface them.")
        out.append("")
        for posting in deferred[:25]:
            out.append("- %s — %s (%s)" % (posting.company, posting.title,
                                           posting.location or "location tbc"))
        out.append("")

    out.append("---")
    out.append("")
    out.append("## What was filtered out")
    out.append("")
    labels = [
        ("dropped_source", "Untrusted source (aggregator, scraper or staffing site)"),
        ("dropped_location", "Failed the hard location filter"),
        ("dropped_location_verified", "Looked in-location, but the live page said otherwise"),
        ("dropped_stale", "Too old"),
        ("dropped_stale_verified", "Too old according to the live page"),
        ("dropped_unverified", "Could not be verified as a real, open listing"),
        ("dropped_undated", "No posting date (you chose to drop undated ones)"),
        ("dropped_requirements", "Failed a salary / employment / clearance / title filter"),
        ("dropped_seen", "Already seen on an earlier run"),
        ("dropped_excluded", "Employer on your exclusion list"),
    ]
    rows = [(label, stats[key]) for key, label in labels if stats.get(key)]
    if rows:
        out.append("| Reason | Count |")
        out.append("|---|---:|")
        for label, count in rows:
            out.append("| %s | %d |" % (label, count))
        out.append("")
        out.append("_%d raw posting(s) read from employer boards, %d survived the "
                   "pre-filters, %d were confirmed live._"
                   % (stats.get("raw", 0), stats.get("survived_prefilter", 0),
                      stats.get("verified", 0)))
        out.append("")

    interesting = [p for p in dropped
                   if p.rejected_reason and not p.rejected_reason.startswith("already")]
    if interesting:
        out.append("<details><summary>The %d individual rejections (not counting "
                   "repeats from earlier runs)</summary>" % len(interesting))
        out.append("")
        out.append("| Employer | Role | Why it was dropped |")
        out.append("|---|---|---|")
        for posting in interesting[:80]:
            out.append("| %s | %s | %s |"
                       % (posting.company or "—", (posting.title or "—")[:60],
                          posting.rejected_reason[:120]))
        out.append("")
        out.append("</details>")
        out.append("")

    if errors:
        out.append("## Problems during the run")
        out.append("")
        for error in errors[:20]:
            out.append("- %s" % error)
        out.append("")

    if usage:
        out.append("---")
        out.append("")
        out.append("_%s_" % usage)
        out.append("")
    return "\n".join(out)


def write(text: str, reports_dir: Path, today: Optional[dt.date] = None) -> Path:
    today = today or dt.date.today()
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / ("%s.md" % today.isoformat())
    suffix = 2
    while path.exists():
        path = reports_dir / ("%s-%d.md" % (today.isoformat(), suffix))
        suffix += 1
    path.write_text(text, encoding="utf-8")
    return path
