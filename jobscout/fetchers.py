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
import urllib.parse
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


def _post_json(url: str, body: Dict[str, Any]) -> Any:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


#: "Posted 12 Days Ago" and friends — Workday's relative dates.
_RELATIVE = re.compile(r"(\d+)\s*\+?\s*(day|week|month|year)", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def _relative_date(text: str, today: Optional[dt.date] = None) -> str:
    """Turn Workday's "Posted 12 Days Ago" into a date.

    "30+ Days Ago" resolves to 31 days, which is deliberately just over the
    default freshness limit: the board is telling us it has stopped counting,
    and a posting it has stopped counting is old.
    """
    today = today or dt.date.today()
    lowered = (text or "").lower()
    if "today" in lowered or "just posted" in lowered:
        return today.isoformat()
    if "yesterday" in lowered:
        return (today - dt.timedelta(days=1)).isoformat()
    match = _RELATIVE.search(lowered)
    if match:
        days = int(match.group(1)) * _UNIT_DAYS[match.group(2).lower()]
        if "+" in lowered:
            days += 1
        return (today - dt.timedelta(days=days)).isoformat()
    return ""


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

def fetch_greenhouse(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
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


def fetch_lever(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
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


def fetch_ashby(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
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


def fetch_smartrecruiters(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
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


def fetch_workable(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
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


#: Locale segments that appear in Workday URLs but are not the site name.
_LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")

#: Workday lists a role's location as "3 Locations" when it spans several. Those
#: have to be resolved individually or the one location you can actually take
#: hides behind the summary — but each is a request, so the number is capped.
WORKDAY_DETAIL_LOOKUPS = 30
WORKDAY_PAGE = 20
WORKDAY_PAGES_PER_QUERY = 3


def _workday_parts(url: str) -> Tuple[str, str, str]:
    """``(host, tenant, site)`` from any shape of Workday careers URL."""
    host = host_of(url)
    tenant = host.split(".")[0] if host else ""
    segments = [s for s in (urlsplit(url).path or "").split("/") if s]
    segments = [s for s in segments if not _LOCALE.match(s)]
    site = segments[0] if segments else ""
    return host, tenant, site


def fetch_workday(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    host, tenant, site = _workday_parts(url)
    if not (host and tenant and site):
        return FetchResult(ok=False, note="could not read the Workday tenant/site "
                                          "out of %s" % url)
    base = "https://%s/wday/cxs/%s/%s" % (host, tenant, site)

    # Workday tenants list thousands of roles, so drive the board's own search
    # with the candidate's target titles rather than paging the whole thing.
    queries = [t for t in (context.get("titles") or [])][:5] or [""]
    seen: Dict[str, Posting] = {}
    raw: Dict[str, str] = {}   # posting url -> externalPath, for detail lookups
    for query in queries:
        for page in range(WORKDAY_PAGES_PER_QUERY):
            body = {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                    "offset": page * WORKDAY_PAGE, "searchText": query}
            data = _post_json(base + "/jobs", body)
            postings = data.get("jobPostings") or []
            for job in postings:
                path = str(job.get("externalPath") or "")
                if not path:
                    continue
                job_url = "https://%s/%s%s" % (host, site, path)
                if job_url in seen:
                    continue
                seen[job_url] = Posting(
                    company=company,
                    title=str(job.get("title") or "").strip(),
                    location=str(job.get("locationsText") or "").strip(),
                    url=job_url,
                    source="Workday",
                    posted=_relative_date(str(job.get("postedOn") or "")),
                )
                raw[job_url] = path
            if len(postings) < WORKDAY_PAGE:
                break

    # Resolve the ambiguous ones: "3 Locations" could well be hiding the one
    # location that matters, and the detail endpoint carries a real date too.
    ambiguous = [p for p in seen.values() if re.match(r"^\d+\s+locations?$",
                                                      p.location.strip(), re.I)]
    for posting in ambiguous[:WORKDAY_DETAIL_LOOKUPS]:
        try:
            detail = _get_json(base + "/job" + raw[posting.url])
        except Exception:
            continue
        info = detail.get("jobPostingInfo") or {}
        places = [str(info.get("location") or "").strip()]
        places += [str(p).strip() for p in (info.get("additionalLocations") or [])]
        places = [p for p in places if p]
        if places:
            posting.location = "; ".join(places[:8])
        if info.get("startDate"):
            posting.posted = _iso_date(info.get("startDate"))
        if info.get("externalUrl"):
            posting.url = str(info["externalUrl"])
        posting.summary = _plain(info.get("jobDescription"))

    postings = list(seen.values())
    return FetchResult(postings=postings, ats="Workday",
                       note="%d role(s) matched on the Workday board" % len(postings))


# --- iCIMS ----------------------------------------------------------------
#
# iCIMS is the exception that proves the rule. It publishes no JSON API, but
# unlike the JavaScript boards it renders its listings SERVER-SIDE — so the jobs
# really are in the HTML, and can be parsed deterministically. Passing
# in_iframe=1 returns the bare list without the site chrome.

ICIMS_PAGES = 4

_ICIMS_JOB = re.compile(
    r'<a href="(?P<url>[^"]*?/jobs/\d+/[^"]*?)"\s+class="iCIMS_Anchor"'
    r'\s+title="(?P<title>[^"]*)"', re.I)
_ICIMS_H3 = re.compile(r"<h3[^>]*>\s*(.*?)\s*</h3>", re.S | re.I)
_ICIMS_LOCATION = re.compile(
    r'glyphicons-map-marker.*?<dd class="iCIMS_JobHeaderData">\s*<span[^>]*>\s*'
    r'(?P<location>[^<]+?)\s*</span>', re.S | re.I)
_ICIMS_DESCRIPTION = re.compile(
    r'<div class="[^"]*description[^"]*">\s*(?P<text>.*?)</div>', re.S | re.I)
#: iCIMS writes locations as "US-<state>-<city>"; make that readable.
_ICIMS_PLACE = re.compile(r"^US-([A-Z]{2})-(.+)$")


def _icims_location(text: str) -> str:
    match = _ICIMS_PLACE.match((text or "").strip())
    if match:
        return "%s, %s" % (match.group(2).strip(), match.group(1))
    return (text or "").strip()


#: iCIMS is paged, so a slow board multiplies its own latency. A shorter budget
#: per page, and stopping at the first timeout, keeps one sluggish employer from
#: holding up everyone else's boards.
HTML_TIMEOUT = 12


def _get_html(url: str) -> str:
    request = urllib.request.Request(url, headers={
        # iCIMS serves a stub to clients it does not recognise as a browser.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscout/0.1",
        "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(request, timeout=HTML_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _parse_icims(company: str, page: str) -> List[Posting]:
    matches = list(_ICIMS_JOB.finditer(page))
    postings = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(page)
        row = page[match.end():end]

        title = match.group("title").strip()
        heading = _ICIMS_H3.search(row)
        if heading:
            title = _plain(heading.group(1), 200) or title
        # The title attribute is "2441 - Accounts Payable Specialist".
        title = re.sub(r"^\d+\s*-\s*", "", title).strip()

        place = _ICIMS_LOCATION.search(row)
        description = _ICIMS_DESCRIPTION.search(row)
        postings.append(Posting(
            company=company,
            title=title,
            location=_icims_location(place.group("location")) if place else "",
            url=match.group("url").split("?")[0],
            source="iCIMS",
            # The iCIMS list view carries no posting date. Leaving it blank is
            # honest; the listing being on the live board is what vouches for it.
            posted="",
            summary=_plain(description.group("text")) if description else "",
        ))
    return postings


def fetch_icims(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    host = host_of(url)
    if not host:
        return FetchResult(ok=False, note="no iCIMS host in the URL")
    base = "https://%s/jobs/search?ss=1&in_iframe=1" % host

    seen: Dict[str, Posting] = {}
    for page_number in range(ICIMS_PAGES):
        try:
            page = _get_html(base + ("&pr=%d" % page_number if page_number else ""))
        except Exception:
            # Keep whatever earlier pages gave us rather than losing the board.
            break
        found = _parse_icims(company, page)
        for posting in found:
            seen.setdefault(posting.url, posting)
        if not found:
            break

    postings = list(seen.values())
    return FetchResult(postings=postings, ats="iCIMS",
                       note="%d role(s) on the iCIMS board" % len(postings))


# --- Breezy and Rippling ---------------------------------------------------

def fetch_breezy(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    host = host_of(url)
    slug = host.split(".")[0] if host else ""
    if not slug:
        return FetchResult(ok=False, note="no Breezy slug in the URL")
    data = _get_json("https://%s.breezy.hr/json" % slug)
    postings = []
    for job in data if isinstance(data, list) else []:
        place = job.get("location") or {}
        parts = [str(place.get("city") or "").strip(),
                 str((place.get("state") or {}).get("id") or "").strip(),
                 str((place.get("country") or {}).get("name") or "").strip()]
        location = ", ".join(p for p in parts if p)
        if place.get("is_remote") and "remote" not in location.lower():
            location = "Remote — %s" % location if location else "Remote"
        postings.append(Posting(
            company=company,
            title=str(job.get("name") or "").strip(),
            location=location,
            url=str(job.get("url") or "").strip(),
            source="Breezy",
            posted=_iso_date(job.get("published_date")),
            salary=str(job.get("salary") or "").strip(),
        ))
    return FetchResult(postings=postings, ats="Breezy",
                       note="%d role(s) on the Breezy board" % len(postings))


def fetch_rippling(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    slug = _slug(url)
    if not slug:
        return FetchResult(ok=False, note="no Rippling board slug in the URL")
    data = _get_json(
        "https://api.rippling.com/platform/api/ats/v1/board/%s/jobs" % slug)
    postings = []
    for job in data if isinstance(data, list) else []:
        place = job.get("workLocation") or {}
        postings.append(Posting(
            company=company,
            title=str(job.get("name") or "").strip(),
            location=str(place.get("label") or place.get("id") or "").strip(),
            url=str(job.get("url") or "").strip(),
            source="Rippling",
            # Rippling's board API carries no posting date at all.
            posted="",
        ))
    return FetchResult(postings=postings, ats="Rippling",
                       note="%d role(s) on the Rippling board" % len(postings))


# --- UltiPro / UKG ---------------------------------------------------------

_ULTIPRO = re.compile(r"/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})")


def fetch_ultipro(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    match = _ULTIPRO.search(url or "")
    if not match:
        return FetchResult(ok=False, note="no UltiPro job-board id in the URL")
    app, board = match.group(1), match.group(2)
    base = "https://recruiting.ultipro.com/%s/JobBoard/%s" % (app, board)
    data = _post_json(base + "/JobBoardView/LoadSearchResults",
                      {"opportunitySearch": {"Top": 200, "Skip": 0, "QueryString": "",
                                             "OrderBy": [], "Filters": []}})
    postings = []
    for job in data.get("opportunities") or []:
        places = [str((p or {}).get("LocalizedDescription") or "").strip()
                  for p in (job.get("Locations") or [])]
        places = [p for p in places if p]
        location = "; ".join(places[:6])
        if str(job.get("JobLocationType") or "").lower() == "remote" \
                and "remote" not in location.lower():
            location = "Remote — %s" % location if location else "Remote"
        postings.append(Posting(
            company=company,
            title=str(job.get("Title") or "").strip(),
            location=location,
            url="%s/OpportunityDetail?opportunityId=%s" % (base, job.get("Id") or ""),
            source="UltiPro",
            posted=_iso_date(job.get("PostedDate")),
            summary=_plain(job.get("BriefDescription")),
        ))
    return FetchResult(postings=postings, ats="UltiPro",
                       note="%d role(s) on the UltiPro board" % len(postings))


# --- finding the ATS hiding behind a careers page --------------------------
#
# A vanity careers domain — jobs.<company>.com, careers.<company>.com — is very
# often a wrapper around a real ATS. NVIDIA's is a Workday board; ARA's simply
# redirects to UltiPro. Left as-is, those employers fall to the slow agent scan
# and return nothing. Following the redirect and reading the page's own links
# turns them into free, complete board reads.

_ATS_LINK = re.compile(
    r"https?://[\w.-]*(?:job-boards\.greenhouse\.io|boards\.greenhouse\.io"
    r"|jobs\.lever\.co|jobs\.ashbyhq\.com|myworkdayjobs\.com"
    r"|jobs\.smartrecruiters\.com|apply\.workable\.com|icims\.com"
    r"|recruiting\.ultipro\.com"
    # The .jobs TLD is restricted to employers, and some use it as their real
    # board while their own careers page is a brochure. `supports()` still
    # gates whether anything is done with the link.
    # Trailing path optional: some careers pages link the bare host.
    r"|[\w-]+\.jobs)(?:/[\w./%-]*)?", re.I)


# --- the national labs, and everything else on a .jobs domain ---------------
#
# The .jobs TLD is restricted to employers, and some large ones — national
# laboratories especially — use it as their real board while their own careers
# page is a brochure. None of it was readable: there is no Workday tenant to
# guess at, and the job pages are JavaScript shells the agent fallback reads as
# empty boards.
#
# What these sites do publish is a sitemap of every open role with a `lastmod`
# date. Some carry the location and title in the path itself
# (`/<city>-<state>/<role-title>/<id>/job/`), which is enough on its own;
# others carry only the title and need one page fetch for the location, which
# the pages give up as schema.org JobPosting JSON-LD. So the date and title
# filters run against the sitemap for free, and only the survivors cost a
# request.

_DOT_JOBS_SITEMAPS = ("/sitemap.xml",)
_SITEMAP_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_URL = re.compile(r"<url>(.*?)</url>", re.I | re.S)
_SITEMAP_LASTMOD = re.compile(r"<lastmod>\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", re.I)
_LDJSON = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
#: `/<city>-<state>/<role-title>/<hex id>/job/` — city, two-letter state.
_DOT_JOBS_PLACE = re.compile(r"^/([a-z][a-z-]+)-([a-z]{2})/", re.I)
#: Trailing path segments that identify rather than describe. Some sites end
#: `/<hex>/job/`, others end `/<uuid>`. Either way the title is the last
#: segment that is not one of these.
_DOT_JOBS_ID = re.compile(r"^(job|jobs|[0-9a-f]{8,}|[0-9a-f-]{30,}|\d+)$", re.I)
#: How many survivors are worth one page fetch each. Reached only by roles that
#: already passed the date and title filters.
_DOT_JOBS_DETAIL_BUDGET = 40


def _titlecase_slug(slug: str) -> str:
    words = [w for w in slug.replace("_", "-").split("-") if w]
    return " ".join(w.upper() if len(w) <= 2 and w.isalpha() and w.lower() in
                    ("hp", "ai", "ml", "qa", "it") else w.capitalize()
                    for w in words)


def _dot_jobs_sitemap_urls(root: str) -> List[Tuple[str, str]]:
    """Every job URL on a .jobs site, with its last-modified date."""
    index = _get_html(root.rstrip("/") + "/sitemap.xml")
    children = [u for u in _SITEMAP_LOC.findall(index)
                if "page" not in u.rsplit("/", 1)[-1].lower()]
    if not children:
        children = [root.rstrip("/") + "/sitemap.xml"]
    out: List[Tuple[str, str]] = []
    for child in children[:4]:
        try:
            body = _get_html(child)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            continue
        for block in _SITEMAP_URL.findall(body):
            loc = _SITEMAP_LOC.search(block)
            if not loc or "/job" not in loc.group(1):
                continue
            when = _SITEMAP_LASTMOD.search(block)
            out.append((loc.group(1), when.group(1) if when else ""))
    return out


def _ldjson_posting(company: str, url: str, page: str) -> Optional[Posting]:
    for block in _LDJSON.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        places = data.get("jobLocation")
        places = places if isinstance(places, list) else [places]
        where = []
        for place in places:
            address = (place or {}).get("address") or {}
            bits = [address.get("addressLocality"), address.get("addressRegion")]
            joined = ", ".join(str(b).strip() for b in bits if b)
            if joined and joined not in where:
                where.append(joined)
        return Posting(
            company=company, title=str(data.get("title") or "").strip(),
            location="; ".join(where), url=url, source=".jobs",
            posted=_iso_date(data.get("datePosted")),
            summary=_plain(data.get("description")))
    return None


def fetch_dot_jobs(company: str, url: str, context: Dict[str, Any]) -> FetchResult:
    root = "https://" + host_of(url)
    entries = _dot_jobs_sitemap_urls(root)
    if not entries:
        return FetchResult(ok=False, note="no job sitemap on this .jobs site")

    titles = [t.lower() for t in (context.get("titles") or []) if t]
    max_age = context.get("max_age_days")
    today = dt.date.today()

    shortlist: List[Tuple[str, str, str, str]] = []   # url, date, title, location
    for job_url, when in entries:
        path = urllib.parse.urlparse(job_url).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue
        place_match = _DOT_JOBS_PLACE.match(path)
        # The title is the last segment that names the role rather than
        # identifying it: some sites end `/<hex>/job/`, others end `/<uuid>`.
        descriptive = [seg for seg in parts if not _DOT_JOBS_ID.match(seg)]
        if place_match and len(descriptive) > 1:
            descriptive = descriptive[1:]        # drop the leading city-state
        slug = descriptive[-1] if descriptive else ""
        if not slug:
            continue
        title = _titlecase_slug(slug)
        location = ("%s, %s" % (_titlecase_slug(place_match.group(1)),
                                place_match.group(2).upper())
                    if place_match else "")
        # Both filters run against the sitemap, before any request is spent.
        if max_age and when:
            try:
                if (today - dt.date.fromisoformat(when)).days > int(max_age):
                    continue
            except ValueError:
                pass
        if titles and not any(word in title.lower() for word in titles):
            continue
        shortlist.append((job_url, when, title, location))

    postings: List[Posting] = []
    detailed = 0
    for job_url, when, title, location in shortlist:
        if location:
            # The path already said where it is; no request needed.
            postings.append(Posting(company=company, title=title,
                                    location=location, url=job_url,
                                    source=".jobs", posted=when))
            continue
        if detailed >= _DOT_JOBS_DETAIL_BUDGET:
            break
        detailed += 1
        try:
            found = _ldjson_posting(company, job_url, _get_html(job_url))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            continue
        if found and found.title:
            found.company = company
            found.posted = found.posted or when
            postings.append(found)

    return FetchResult(
        postings=postings, ats=".jobs",
        note="%d of %d role(s) matched from the .jobs sitemap"
             % (len(postings), len(entries)))


def discover_ats(url: str) -> str:
    """The ATS board behind a careers page, or "" if there is none.

    Follows the redirect first — some careers domains simply forward to the ATS
    — then reads the page's own links.
    """
    if not url or supports(url):
        return url or ""
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) jobscout/0.1",
            "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            final = response.geturl()
            page = response.read(600_000).decode("utf-8", errors="replace")
    except Exception:
        return ""

    if supports(final):
        return final
    for candidate in _ATS_LINK.findall(page):
        cleaned = candidate.rstrip("\"'&;,)")
        # Trim a deep link back to the board itself.
        for marker in ("/login", "/apply", "/job/", "/jobs/"):
            index = cleaned.lower().find(marker)
            if index > 0:
                cleaned = cleaned[:index]
                break
        if supports(cleaned):
            return cleaned
    return ""


#: host suffix -> fetcher. Order does not matter; hosts are disjoint.
FETCHERS: Tuple[Tuple[str, Callable[[str, str, Dict[str, Any]], FetchResult]], ...] = (
    ("boards.greenhouse.io", fetch_greenhouse),
    ("job-boards.greenhouse.io", fetch_greenhouse),
    ("jobs.lever.co", fetch_lever),
    ("jobs.ashbyhq.com", fetch_ashby),
    ("ashbyhq.com", fetch_ashby),
    ("jobs.smartrecruiters.com", fetch_smartrecruiters),
    ("careers.smartrecruiters.com", fetch_smartrecruiters),
    ("apply.workable.com", fetch_workable),
    ("myworkdayjobs.com", fetch_workday),
    ("myworkdaysite.com", fetch_workday),
    ("icims.com", fetch_icims),
    ("recruiting.ultipro.com", fetch_ultipro),
    ("breezy.hr", fetch_breezy),
    ("ats.rippling.com", fetch_rippling),
    # Any employer on the .jobs TLD. Naming individual employers here would put
    # one person's shortlist in the shipped code; the TLD is restricted to
    # employers, and a .jobs site without a usable sitemap falls back to the
    # agent scan like any other unreadable board.
    ("jobs", fetch_dot_jobs),
)


def supports(url: str) -> bool:
    host = host_of(url)
    return any(_host_matches(host, candidate) for candidate, _ in FETCHERS)


def fetch(company: str, url: str,
          context: Optional[Dict[str, Any]] = None) -> Optional[FetchResult]:
    """Read a board directly. ``None`` means "no API for this host, use the agent".

    ``context`` may carry ``titles`` — the candidate's target job titles — which
    boards that are too big to read whole (Workday) use to drive their own search.
    """
    host = host_of(url)
    for candidate, fetcher in FETCHERS:
        if not _host_matches(host, candidate):
            continue
        try:
            return fetcher(company, url, context or {})
        except urllib.error.HTTPError as exc:
            # A 404 usually means the slug is wrong, which is worth surfacing:
            # the employer's board may simply live under a different handle.
            return FetchResult(ok=False, note="board API returned HTTP %d — the "
                                              "board slug may be wrong" % exc.code)
        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            return FetchResult(ok=False, note="board API unreachable: %s" % str(exc)[:120])
    return None
