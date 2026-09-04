"""Reading applicant-tracking boards directly, with no model involved.

The first version of this tool asked an agent to fetch each careers page and
report what was on it. That produced almost nothing, and the reason is
structural: Greenhouse, Lever, Ashby, Workday and iCIMS boards are JavaScript
applications. Fetching one returns an empty shell, so the agent honestly
reported an empty board — ten boards, one posting, four dollars.

But every one of those systems publishes the same listings as free, public JSON.
So jobscout asks the API instead:

    Greenhouse       boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true
    Lever            api.lever.co/v0/postings/<slug>?mode=json
    Ashby            api.ashbyhq.com/posting-api/job-board/<slug>
    SmartRecruiters  api.smartrecruiters.com/v1/companies/<slug>/postings
    Workable         apply.workable.com/api/v1/widget/accounts/<slug>

This is strictly better in every direction that matters. It is free and instant.
It returns the *complete* board rather than whatever survived a page fetch. The
posting dates and location strings are the employer's own fields rather than a
model's reading of them. And a hallucinated job is not merely unlikely — it is
impossible, because nothing here is generated.

The agent-driven scan in :mod:`jobscout.agents` remains, for in-house and
government boards that have no API. It is the fallback now, not the default.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .models import Posting
from .sources import _host_matches, host_of

USER_AGENT = "jobscout/0.1 (+https://github.com/deknapp/jobscout)"
TIMEOUT = 25
SUMMARY_CHARS = 600

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


@dataclass
class FetchResult:
    postings: List[Posting] = field(default_factory=list)
    ats: str = ""
    note: str = ""
    ok: bool = True


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _plain(text: Optional[str], limit: int = SUMMARY_CHARS) -> str:
    if not text:
        return ""
    # Greenhouse serves its job content HTML-escaped, so unescape BEFORE
    # stripping tags or the markup survives as literal text; unescape again
    # afterwards for entities inside the text itself (&amp;nbsp;, &amp;amp;).
    stripped = html.unescape(text)
    stripped = html.unescape(_TAGS.sub(" ", stripped))
    stripped = _SPACE.sub(" ", stripped).strip()
    return stripped[:limit].rstrip()


def _iso_date(value: Any) -> str:
    """Normalise the several date shapes these APIs use to YYYY-MM-DD."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds.
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return dt.datetime.utcfromtimestamp(seconds).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value)
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    return match.group(0) if match else ""


def _slug(url: str, depth: int = 1) -> str:
    parts = [p for p in (urlsplit(url).path or "").split("/") if p]
    return parts[depth - 1] if len(parts) >= depth else ""


# --- one function per ATS --------------------------------------------------

def fetch_greenhouse(company: str, url: str) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no Greenhouse board slug in the URL")
    data = _get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug)
    postings = []
    for job in data.get("jobs", []) or []:
        postings.append(Posting(
            company=company,
            title=str(job.get("title") or "").strip(),
            location=str((job.get("location") or {}).get("name") or "").strip(),
            url=str(job.get("absolute_url") or "").strip(),
            source="Greenhouse",
            posted=_iso_date(job.get("first_published") or job.get("updated_at")),
            summary=_plain(job.get("content")),
        ))
    return FetchResult(postings=postings, ats="Greenhouse",
                       note="%d role(s) on the Greenhouse board" % len(postings))


def fetch_lever(company: str, url: str) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no Lever board slug in the URL")
    data = _get_json("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    postings = []
    for job in data if isinstance(data, list) else []:
        categories = job.get("categories") or {}
        location = str(categories.get("location") or "").strip()
        workplace = str(job.get("workplaceType") or "").strip().lower()
        if workplace and workplace not in location.lower():
            location = ("%s (%s)" % (location, workplace)).strip()
        postings.append(Posting(
            company=company,
            title=str(job.get("text") or "").strip(),
            location=location,
            url=str(job.get("hostedUrl") or job.get("applyUrl") or "").strip(),
            source="Lever",
            posted=_iso_date(job.get("createdAt")),
            summary=_plain(job.get("descriptionPlain") or job.get("description")),
        ))
    return FetchResult(postings=postings, ats="Lever",
                       note="%d role(s) on the Lever board" % len(postings))


def fetch_ashby(company: str, url: str) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no Ashby board slug in the URL")
    data = _get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    postings = []
    for job in data.get("jobs", []) or []:
        if job.get("isListed") is False:
            continue
        location = str(job.get("location") or "").strip()
        extra = [str((s or {}).get("location") or "").strip()
                 for s in (job.get("secondaryLocations") or [])]
        extra = [e for e in extra if e and e.lower() != location.lower()]
        if extra:
            location = "%s; also %s" % (location, ", ".join(extra[:6]))
        if job.get("isRemote") and "remote" not in location.lower():
            location = "Remote — %s" % location if location else "Remote"
        postings.append(Posting(
            company=company,
            title=str(job.get("title") or "").strip(),
            location=location,
            url=str(job.get("jobUrl") or job.get("applyUrl") or "").strip(),
            source="Ashby",
            posted=_iso_date(job.get("publishedAt")),
            summary=_plain(job.get("descriptionPlain") or job.get("descriptionHtml")),
        ))
    return FetchResult(postings=postings, ats="Ashby",
                       note="%d role(s) on the Ashby board" % len(postings))


def fetch_smartrecruiters(company: str, url: str) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no SmartRecruiters slug in the URL")
    data = _get_json(
        "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100" % slug)
    postings = []
    for job in data.get("content", []) or []:
        place = job.get("location") or {}
        location = str(place.get("fullLocation") or " ".join(
            str(place.get(k) or "") for k in ("city", "region", "country"))).strip()
        if place.get("remote") and "remote" not in location.lower():
            location = "Remote — %s" % location if location else "Remote"
        job_id = str(job.get("id") or "")
        postings.append(Posting(
            company=company,
            title=str(job.get("name") or "").strip(),
            location=location,
            url="https://jobs.smartrecruiters.com/%s/%s" % (slug, job_id) if job_id else "",
            source="SmartRecruiters",
            posted=_iso_date(job.get("releasedDate")),
        ))
    return FetchResult(postings=postings, ats="SmartRecruiters",
                       note="%d role(s) on the SmartRecruiters board" % len(postings))


def fetch_workable(company: str, url: str) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no Workable slug in the URL")
    data = _get_json(
        "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % slug)
    postings = []
    for job in data.get("jobs", []) or []:
        place = job.get("location") or {}
        location = ", ".join(str(place.get(k) or "").strip()
                             for k in ("city", "region", "country")
                             if str(place.get(k) or "").strip())
        if job.get("telecommuting") and "remote" not in location.lower():
            location = "Remote — %s" % location if location else "Remote"
        postings.append(Posting(
            company=company,
            title=str(job.get("title") or "").strip(),
            location=location,
            url=str(job.get("url") or job.get("application_url") or "").strip(),
            source="Workable",
            posted=_iso_date(job.get("published_on") or job.get("created_at")),
            summary=_plain(job.get("description")),
        ))
    return FetchResult(postings=postings, ats="Workable",
                       note="%d role(s) on the Workable board" % len(postings))


#: host suffix -> fetcher. Order does not matter; hosts are disjoint.
FETCHERS: Tuple[Tuple[str, Callable[[str, str], FetchResult]], ...] = (
    ("boards.greenhouse.io", fetch_greenhouse),
    ("job-boards.greenhouse.io", fetch_greenhouse),
    ("jobs.lever.co", fetch_lever),
    ("jobs.ashbyhq.com", fetch_ashby),
    ("ashbyhq.com", fetch_ashby),
    ("jobs.smartrecruiters.com", fetch_smartrecruiters),
    ("careers.smartrecruiters.com", fetch_smartrecruiters),
    ("apply.workable.com", fetch_workable),
)


def supports(url: str) -> bool:
    host = host_of(url)
    return any(_host_matches(host, candidate) for candidate, _ in FETCHERS)


def fetch(company: str, url: str) -> Optional[FetchResult]:
    """Read a board directly. ``None`` means "no API for this host, use the agent"."""
    host = host_of(url)
    for candidate, fetcher in FETCHERS:
        if not _host_matches(host, candidate):
            continue
        try:
            return fetcher(company, url)
        except urllib.error.HTTPError as exc:
            # A 404 usually means the slug is wrong, which is worth surfacing:
            # the employer's board may simply live under a different handle.
            return FetchResult(ok=False, note="board API returned HTTP %d — the "
                                              "board slug may be wrong" % exc.code)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            return FetchResult(ok=False, note="board API unreachable: %s" % str(exc)[:120])
    return None
