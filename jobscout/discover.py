"""Finding an employer's careers board without asking a model.

Board discovery used to be a model call with web search and web fetch. That is
an expensive way to answer a question that is usually mechanical: most
employers do not host a careers board at all, they rent one, and the rented
ones live at predictable addresses. Acme on Greenhouse is
``boards.greenhouse.io/acme``, and the API behind it will say so for the cost
of one HTTP request.

Three attempts, cheapest first:

1. **Probe the ATS APIs** with slugs derived from the name. A 200 with real
   postings is not a guess, it is the board.
2. **Read their own site.** Homepages link to their careers page, and that
   page nearly always links to the ATS. Two fetches, no model.
3. **Give up and let the caller ask a model.** Rare, and the answer is cached
   forever afterwards -- a company's careers board does not move.

The reason this is worth doing is not elegance. Every model call in this stage
carried up to eight web searches at $0.01 each plus whatever the fetched pages
cost as input tokens, and it ran once per unresolved employer.

**A wrong answer here is worse than no answer**, because it gets cached and
then silently supplies another company's jobs forever. So a slug that is a
truncation of the name is only accepted when the board itself confirms whose
it is; anything less certain is left for the model.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from .corpus import normalize_company

USER_AGENT = "jobscout/0.1 (+https://github.com/deknapp/jobscout)"
TIMEOUT = 15

#: Ceiling on HTTP requests spent per employer. Probing is cheap but not free,
#: and the boards being probed belong to other people.
MAX_PROBES = 14

Getter = Callable[[str], Tuple[int, str]]


def _http_get(url: str) -> Tuple[int, str]:
    """Return (status, body). Never raises for an HTTP error status."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


@dataclass
class BoardGuess:
    url: str
    ats: str
    slug: str
    #: ``api`` -- an ATS API returned this company's postings.
    #: ``named`` -- the ATS also confirmed the board's own name matches.
    #: ``link`` -- found by following a careers link on the employer's site.
    how: str
    jobs_seen: int = 0
    probes: int = 0
    checked: List[str] = field(default_factory=list)


# --- slugs -----------------------------------------------------------------

_PUNCT = re.compile(r"[^a-z0-9]+")
#: Suffixes that are part of a legal name and never part of a board slug.
_LEGAL = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|"
                    r"gmbh|sa|nv|bv|ag|pty|holdings)\b\.?", re.I)


def slugs_for(name: str) -> List[str]:
    """Candidate ATS handles for a company name, most specific first.

    Order matters and is the safety property: the full name compressed is
    almost never someone else's board, while the first word very often is.
    ``Descartes Labs`` -> descarteslabs, descartes-labs, descartes -- and that
    last one is only ever accepted with confirmation.
    """
    cleaned = _LEGAL.sub(" ", name or "")
    words = [w for w in _PUNCT.sub(" ", cleaned.lower()).split() if w]
    if not words:
        return []
    out: List[str] = []

    def add(slug: str) -> None:
        if slug and slug not in out and len(slug) > 1:
            out.append(slug)

    add("".join(words))
    add("-".join(words))
    if len(words) > 1:
        add(words[0])
    return out[:3]


def is_specific(slug: str, name: str) -> bool:
    """Does this slug use the whole name, or only the front of it?

    A truncated slug is the one that finds someone else's board.
    """
    cleaned = _LEGAL.sub(" ", name or "")
    words = [w for w in _PUNCT.sub(" ", cleaned.lower()).split() if w]
    return len(words) <= 1 or slug in {"".join(words), "-".join(words)}


# --- the ATS probes --------------------------------------------------------

def _greenhouse(slug: str, get: Getter) -> Optional[Tuple[str, int, str]]:
    status, body = get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    if status != 200:
        return None
    try:
        jobs = json.loads(body).get("jobs") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    return ("https://boards.greenhouse.io/%s" % slug, len(jobs), "") if jobs else None


def _greenhouse_name(slug: str, get: Getter) -> str:
    """Greenhouse publishes the board's own name, which settles ownership."""
    status, body = get("https://boards-api.greenhouse.io/v1/boards/%s" % slug)
    if status != 200:
        return ""
    try:
        return str(json.loads(body).get("name") or "")
    except (json.JSONDecodeError, AttributeError):
        return ""


def _lever(slug: str, get: Getter) -> Optional[Tuple[str, int, str]]:
    status, body = get("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    if status != 200:
        return None
    try:
        posts = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(posts, list) or not posts:
        return None
    return "https://jobs.lever.co/%s" % slug, len(posts), ""


def _ashby(slug: str, get: Getter) -> Optional[Tuple[str, int, str]]:
    status, body = get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    if status != 200:
        return None
    try:
        jobs = json.loads(body).get("jobs") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    return ("https://jobs.ashbyhq.com/%s" % slug, len(jobs), "") if jobs else None


def _smartrecruiters(slug: str, get: Getter) -> Optional[Tuple[str, int, str]]:
    status, body = get(
        "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=10" % slug)
    if status != 200:
        return None
    try:
        content = json.loads(body).get("content") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    return ("https://jobs.smartrecruiters.com/%s" % slug, len(content), "") if content else None


def _workable(slug: str, get: Getter) -> Optional[Tuple[str, int, str]]:
    status, body = get(
        "https://apply.workable.com/api/v1/widget/accounts/%s?details=true" % slug)
    if status != 200:
        return None
    try:
        jobs = json.loads(body).get("jobs") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    return ("https://apply.workable.com/%s/" % slug, len(jobs), "") if jobs else None


#: Ordered by how often employers in this corpus actually use them, because
#: the probe budget is spent in this order.
PROBES: Sequence[Tuple[str, Callable]] = (
    ("Greenhouse", _greenhouse),
    ("Lever", _lever),
    ("Ashby", _ashby),
    ("Workable", _workable),
    ("SmartRecruiters", _smartrecruiters),
)


def probe_ats(name: str, get: Getter = _http_get) -> Optional[BoardGuess]:
    """Try the rented boards. Returns None rather than a guess it cannot stand up."""
    checked: List[str] = []
    probes = 0
    for slug in slugs_for(name):
        for ats, probe in PROBES:
            if probes >= MAX_PROBES:
                return None
            probes += 1
            checked.append("%s:%s" % (ats, slug))
            hit = probe(slug, get)
            if hit is None:
                continue
            url, count, _ = hit

            # Greenhouse publishes the board's own name, so ownership is a
            # fact rather than an inference. Always ask -- including for a
            # slug that looks specific, because a one-word company name looks
            # specific and is the least safe case there is. `General` matched
            # a Greenhouse board on the first probe; the board is called
            # "General Interest" and belongs to nobody.
            if ats == "Greenhouse":
                probes += 1
                board_name = _greenhouse_name(slug, get)
                if not board_name:
                    continue
                if normalize_company(board_name) != normalize_company(name):
                    continue
                return BoardGuess(url=url, ats=ats, slug=slug, how="named",
                                  jobs_seen=count, probes=probes, checked=checked)

            # The others do not say whose board it is, so the slug has to
            # carry the whole name. A truncation that finds a board is the
            # answer that gets cached and then quietly serves someone else's
            # jobs forever.
            if is_specific(slug, name):
                return BoardGuess(url=url, ats=ats, slug=slug, how="api",
                                  jobs_seen=count, probes=probes, checked=checked)
    return None


# --- reading the employer's own site ---------------------------------------

_LINK = re.compile(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_CAREERS = re.compile(r"career|jobs|join[-\s_]?us|work[-\s_]?(with|for|at)[-\s_]?us|"
                      r"vacanc|opportunit|employment|hiring", re.I)

#: Hosts that are an ATS, mapped to the name jobscout records.
_ATS_BY_HOST = {
    "boards.greenhouse.io": "Greenhouse", "job-boards.greenhouse.io": "Greenhouse",
    "jobs.lever.co": "Lever", "jobs.ashbyhq.com": "Ashby",
    "apply.workable.com": "Workable", "jobs.smartrecruiters.com": "SmartRecruiters",
    "boards.eu.greenhouse.io": "Greenhouse", "breezy.hr": "Breezy",
    "recruitee.com": "Recruitee", "jobs.jobvite.com": "Jobvite",
    "myworkdayjobs.com": "Workday", "icims.com": "iCIMS",
    "taleo.net": "Taleo", "successfactors.com": "SuccessFactors",
    "jobs.smartrecruiters.com/": "SmartRecruiters",
}


def _ats_for_host(host: str) -> str:
    host = host.lower()
    for needle, ats in _ATS_BY_HOST.items():
        if host == needle or host.endswith("." + needle) or needle in host:
            return ats
    return ""


def _careers_links(html: str, base: str) -> List[str]:
    found: List[str] = []
    for href, label in _LINK.findall(html or ""):
        text = _TAGS.sub(" ", label)
        if _CAREERS.search(href) or _CAREERS.search(text):
            url = urljoin(base, href.strip())
            if url.startswith("http") and url not in found:
                found.append(url)
    return found


def crawl_careers(homepage: str, get: Getter = _http_get) -> Optional[BoardGuess]:
    """Follow the employer's own careers link and see where it lands.

    One hop, then one more. Employers link 'Careers' from the homepage, and
    that page links the ATS -- so the board is two fetches away, and neither
    of them is a model call.
    """
    if not homepage:
        return None
    probes = 0
    status, html = get(homepage)
    probes += 1
    if status != 200 or not html:
        return None

    first = _careers_links(html, homepage)[:3]
    for link in first:
        ats = _ats_for_host(urlparse(link).netloc)
        if ats:
            return BoardGuess(url=link, ats=ats, slug="", how="link", probes=probes)

    for link in first:
        if probes >= 6:
            break
        probes += 1
        status, page = get(link)
        if status != 200 or not page:
            continue
        for onward in _careers_links(page, link)[:20]:
            ats = _ats_for_host(urlparse(onward).netloc)
            if ats:
                return BoardGuess(url=onward, ats=ats, slug="", how="link", probes=probes)
        # Their own careers page, with no rented board behind it. Still a
        # board -- jobscout can read it -- just not a free one.
        return BoardGuess(url=link, ats="in-house", slug="", how="link", probes=probes)
    return None


def find_board(name: str, homepage: str = "", get: Getter = _http_get) -> Optional[BoardGuess]:
    """Deterministic board discovery. None means 'ask a model'."""
    guess = probe_ats(name, get)
    if guess is not None:
        return guess
    return crawl_careers(homepage, get) if homepage else None
