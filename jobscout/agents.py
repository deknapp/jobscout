"""The agents.

Six narrow jobs, each with one prompt, each returning JSON that Python validates:

1. :func:`build_profile`     read your existing applications -> who you are
2. :func:`propose_companies` who could plausibly want you, in your geography
3. :func:`resolve_board`     find that employer's real careers board, once
4. :func:`scan_board`        read the board and list roles actually on it
5. :func:`verify_posting`    fetch each posting and confirm it is open and real
6. :func:`rank_postings`     score fit against your background, with reasons

The agents are never the authority on location, freshness or source quality —
:mod:`jobscout.filters` and :mod:`jobscout.sources` re-check all three in Python
after every stage. The prompts still state the rules, because an agent that
knows the constraint wastes fewer searches, not because it is trusted to obey.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Sequence

from .companies import Company
from .config import LocationPolicy, Settings
from .corpus import Corpus, Document
from .llm import LLM, LLMError
from .models import Posting
from .sources import ats_search_hints, trusted_source_summary

SYSTEM = (
    "You are a research assistant helping one person find jobs worth applying to. "
    "You are precise and you never invent facts. If you did not see something on a "
    "page you actually fetched, you say so instead of guessing. A confidently "
    "hallucinated job posting wastes a real person's time when they are already "
    "under pressure, so an honest empty answer is always better than a plausible "
    "fabricated one."
)

#: How much of each document goes into the profile prompt.
DOC_EXCERPT_CHARS = 6000
MAX_PROFILE_DOCS = 24


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _policy_block(policy: LocationPolicy) -> str:
    lines = ["HARD LOCATION RULE (non-negotiable — a role that fails this is worthless):"]
    if policy.allowed_states:
        names = ", ".join(policy.allowed_states)
        lines.append("  * Onsite or hybrid roles are acceptable ONLY in: %s." % names)
    if policy.allowed_cities:
        lines.append("  * Specifically including: %s."
                     % ", ".join(sorted(set(policy.allowed_cities))))
    if policy.allow_remote:
        lines.append("  * Fully remote roles are acceptable, BUT many 'remote' roles are "
                     "fenced to states the candidate does not live in ('remote — must "
                     "reside in California'). Those are NOT acceptable. Always report the "
                     "posting's exact stated location text so the fence can be checked.")
    else:
        lines.append("  * Remote roles are NOT acceptable.")
    if not policy.allow_hybrid:
        lines.append("  * Hybrid roles outside the accepted area are NOT acceptable — "
                     "the candidate is not relocating.")
    lines.append("  * The candidate is NOT relocating. Do not suggest roles that would "
                 "require moving, however good the role is.")
    return "\n".join(lines)


def _source_block() -> str:
    return (
        "HARD SOURCE RULE:\n"
        "  * Only postings on %s are acceptable.\n"
        "  * NEVER cite Indeed, LinkedIn, ZipRecruiter, Glassdoor, Dice, Talent.com, "
        "Builtin, Wellfound, staffing agencies or any job aggregator. Those listings "
        "are stale, duplicated or fabricated, and the URLs rot.\n"
        "  * Every URL you give must be one you actually fetched or that appeared "
        "verbatim in a search result. Never construct a URL from a pattern."
        % trusted_source_summary()
    )


# --- 1. profile ------------------------------------------------------------

def _profile_documents(corpus: Corpus) -> List[Document]:
    """Prefer resumes and cover letters; they say the most per character."""
    ranked = {"resume": 0, "cover_letter": 1, "job_description": 2,
              "document": 3, "correspondence": 4, "notes": 5}
    docs = [d for d in corpus.documents if d.text and not d.error]
    docs.sort(key=lambda d: (ranked.get(d.kind, 9), -len(d.text)))
    return docs[:MAX_PROFILE_DOCS]


def build_profile(llm: LLM, corpus: Corpus) -> Dict[str, Any]:
    """Infer the candidate profile from the applications already written."""
    docs = _profile_documents(corpus)
    if not docs:
        raise LLMError("no readable documents in the applications folder")

    blocks = []
    for doc in docs:
        blocks.append("### %s / %s (%s)\n%s"
                      % (doc.company or "unfiled", doc.name, doc.kind,
                         doc.excerpt(DOC_EXCERPT_CHARS)))
    applied = ", ".join(corpus.company_names()) or "none identified"

    prompt = """Below are the job-application materials one candidate has already written:
resumes, cover letters, and the job descriptions they were targeting.

Read them and build a profile of this candidate — what they can do, what level
they are at, and what they are evidently aiming for. Infer target roles from the
jobs they actually applied to, not from what their resume says in the abstract.

Companies they have already applied to: %s

Return ONLY this JSON object:

{
  "headline": "one line describing the candidate professionally",
  "years_experience": <integer, best estimate>,
  "seniority": "one of: junior | mid | senior | staff | principal | manager | director",
  "core_skills": ["the 8-15 skills that actually appear across the materials"],
  "domains": ["industries/problem areas they have real experience in"],
  "target_titles": ["6-10 exact job titles to search for, based on what they applied to"],
  "adjacent_titles": ["4-8 titles that are a plausible stretch or pivot"],
  "employer_types": ["kinds of organisation that hire this profile, e.g. national labs, defence primes, biotech, remote-first SaaS"],
  "differentiators": ["what makes this candidate unusual or hard to replace"],
  "seniority_floor": "the least senior title worth their time",
  "avoid": ["kinds of role the materials suggest they do NOT want"],
  "notes": "anything else a recruiter-sized brain should know, 2-3 sentences"
}

MATERIALS
=========
%s""" % (applied, "\n\n".join(blocks))

    data = llm.ask_json(prompt, strong=True, system=SYSTEM)
    if not isinstance(data, dict):
        raise LLMError("profile agent returned %s, expected an object" % type(data).__name__)
    data["generated"] = dt.date.today().isoformat()
    data["source_documents"] = len(docs)
    data["applied_companies"] = corpus.company_names()
    return data


# --- 2. company generation -------------------------------------------------

def propose_companies(llm: LLM, profile: Dict[str, Any], policy: LocationPolicy,
                      known: Sequence[str], count: int = 25) -> List[Company]:
    """Name employers who could plausibly hire this candidate, in this geography.

    This is the step that makes the tool work: instead of trawling the open web
    for postings, it decides *who to ask*, and later stages read those employers'
    own boards.
    """
    known_block = ", ".join(known) if known else "none yet"
    prompt = """Given the candidate profile below, propose %d SPECIFIC, REAL employers who
could plausibly hire this person, and whose jobs would satisfy the location rule.

%s

Use web search to ground your list in reality. Good sources of candidates:
  * major employers physically located in the accepted area (national labs,
    universities, hospitals, utilities, defence contractors, state agencies,
    local startups and scale-ups)
  * remote-first companies in the candidate's domains that hire nationwide
  * companies whose products or research match the candidate's differentiators

Rules:
  * Real organisations only, with a name you have seen on the web. No invented names.
  * Do NOT repeat any of these, which are already on the list: %s
  * Prefer employers who are plausibly hiring NOW over famous names who are not.
  * Spread the list: do not return 20 national labs or 20 AI startups.
  * For each, say concretely WHY this candidate fits — reference their actual
    skills or domains, not generic praise.

Return ONLY a JSON array:

[
  {
    "name": "exact legal or common name of the employer",
    "why": "one or two sentences tying THIS candidate's background to THIS employer",
    "presence": "how they satisfy the location rule, e.g. 'HQ in Albuquerque, NM' or 'remote-first, hires across the US'",
    "hiring_signal": "any evidence you saw that they are hiring, or '' if none"
  }
]

CANDIDATE PROFILE
=================
%s""" % (count, _policy_block(policy), known_block,
         json.dumps(profile, indent=2)[:6000])

    data = llm.ask_json(prompt, strong=True, system=SYSTEM, web=True)
    if isinstance(data, dict):
        data = data.get("companies") or data.get("employers") or []
    companies: List[Company] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        companies.append(Company(
            name=name,
            why=str(item.get("why") or "").strip(),
            presence=str(item.get("presence") or "").strip(),
            note=str(item.get("hiring_signal") or "").strip(),
        ))
    return companies


# --- 3. careers-board resolution ------------------------------------------

def resolve_board(llm: LLM, company: Company) -> Dict[str, str]:
    """Find the one URL where this employer actually lists its open roles."""
    prompt = """Find the OFFICIAL page where "%s" lists its current job openings.

%s

What counts as the right answer, best first:
  1. The employer's applicant-tracking board — e.g. boards.greenhouse.io/<slug>,
     jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>, <slug>.myworkdayjobs.com/...,
     jobs.smartrecruiters.com/<slug>, careers.<company>.com
  2. For a lab, agency or university: the careers section on their .gov/.edu site.

Verify the page exists by fetching it. If you cannot find a real board, say so —
"" is a correct and useful answer, an invented URL is not.

Return ONLY:
{
  "careers_url": "the URL, or \\"\\" if you could not find one",
  "ats": "which system it is (Greenhouse, Lever, Ashby, Workday, iCIMS, in-house, ...) or \\"\\"",
  "note": "what you saw, or why you could not find it — one short sentence"
}

Context on this employer: %s""" % (company.name, _source_block(),
                                   company.why or company.presence or "(none)")

    data = llm.ask_json(prompt, system=SYSTEM, web=True)
    if not isinstance(data, dict):
        return {"careers_url": "", "ats": "", "note": "unusable response"}
    return {
        "careers_url": str(data.get("careers_url") or "").strip(),
        "ats": str(data.get("ats") or "").strip(),
        "note": str(data.get("note") or "").strip(),
    }


# --- 4. board scan ---------------------------------------------------------

def scan_board(llm: LLM, company: Company, profile: Dict[str, Any],
               policy: LocationPolicy, max_age_days: int) -> List[Posting]:
    """Read one employer's board and list the open roles that fit."""
    titles = _as_list(profile.get("target_titles")) + _as_list(profile.get("adjacent_titles"))
    skills = _as_list(profile.get("core_skills"))
    prompt = """Fetch this employer's job board and list the currently-open roles that fit
the candidate below.

EMPLOYER: %s
BOARD: %s

%s

%s

FRESHNESS: only include roles that are open right now. Ignore anything posted
more than %d days ago. If the board shows a posting date, report it exactly; if
it does not, leave "posted" as "".

WHAT FITS: titles like %s; work involving %s. Seniority around "%s". Close
matches count — an exact title match is not required — but a role the candidate
plainly cannot do, or that is far below their level, does not.

METHOD: actually fetch the board. If it paginates or has a search box, fetch the
filtered/next pages too. For each matching role, give the direct URL to that
specific posting on that same site.

CRITICAL: list only roles you SAW on a page you fetched. If the board has no
matching roles, return []. An empty array is a good answer. Do not fill space.

Return ONLY a JSON array:

[
  {
    "title": "exact title as posted",
    "location": "exact location text as posted, verbatim, including any remote restrictions",
    "url": "direct link to this posting",
    "posted": "YYYY-MM-DD if shown, else \\"\\"",
    "salary": "as posted, else \\"\\"",
    "summary": "2-3 sentences on what the role actually involves"
  }
]""" % (company.name, company.careers_url, _policy_block(policy), _source_block(),
        max_age_days, ", ".join(titles[:12]) or "(unspecified)",
        ", ".join(skills[:12]) or "(unspecified)",
        profile.get("seniority") or "senior")

    data = llm.ask_json(prompt, system=SYSTEM, web=True)
    if isinstance(data, dict):
        data = data.get("postings") or data.get("jobs") or data.get("roles") or []
    postings: List[Posting] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        postings.append(Posting(
            company=company.name,
            title=title,
            location=str(item.get("location") or "").strip(),
            url=url,
            source=company.ats or "board",
            posted=str(item.get("posted") or "").strip(),
            salary=str(item.get("salary") or "").strip(),
            summary=str(item.get("summary") or "").strip(),
        ))
    return postings


# --- 5. verification -------------------------------------------------------

def verify_posting(llm: LLM, posting: Posting) -> Dict[str, str]:
    """Fetch the posting and confirm it is a real, open role that matches.

    This is the pass that catches a confabulated URL, a role that closed last
    month, and a listing whose real location differs from the summary.
    """
    prompt = """Fetch this URL and tell me what is actually on the page.

URL: %s
It is claimed to be: "%s" at %s, located "%s".

Answer strictly from the fetched page. Do not fill gaps from memory or from
search results about the company.

Return ONLY:
{
  "status": "live | dead | mismatch | unreachable",
  "actual_title": "the job title on the page, or \\"\\"",
  "actual_location": "the location text on the page verbatim, including any remote restriction, or \\"\\"",
  "posted": "the posting date on the page as YYYY-MM-DD, or \\"\\"",
  "closes": "application deadline if shown as YYYY-MM-DD, or \\"\\"",
  "note": "one sentence: what you saw"
}

Meaning of each status:
  live       — the page shows this job, open for applications
  dead       — the page loads but the job is closed, filled, expired or gone
  mismatch   — the page shows a DIFFERENT job than claimed
  unreachable— the page would not load, or blocked you""" % (
        posting.url, posting.title, posting.company, posting.location or "unstated")

    try:
        data = llm.ask_json(prompt, system=SYSTEM, web=True)
    except LLMError as exc:
        return {"status": "unreachable", "note": str(exc)[:200]}
    if not isinstance(data, dict):
        return {"status": "unreachable", "note": "unusable response"}
    status = str(data.get("status") or "unreachable").strip().lower()
    if status not in ("live", "dead", "mismatch", "unreachable"):
        status = "unreachable"
    return {
        "status": status,
        "actual_title": str(data.get("actual_title") or "").strip(),
        "actual_location": str(data.get("actual_location") or "").strip(),
        "posted": str(data.get("posted") or "").strip(),
        "closes": str(data.get("closes") or "").strip(),
        "note": str(data.get("note") or "").strip(),
    }


# --- 6. ranking ------------------------------------------------------------

def rank_postings(llm: LLM, postings: Sequence[Posting],
                  profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Score the survivors against the candidate's actual background."""
    if not postings:
        return {}
    listing = [
        {"id": p.id, "company": p.company, "title": p.title, "location": p.location,
         "posted": p.posted, "salary": p.salary, "summary": p.summary}
        for p in postings
    ]
    prompt = """Score each of these verified, in-location job postings against the candidate.

Be honest and discriminating. If a role is a weak fit, score it low and say why —
the candidate is deciding where to spend real hours writing an application, and a
list where everything is a 9 is useless to them.

For each posting return:
  fit_score   0-100. 80+ = apply now. 60-79 = worth a look. below 50 = probably skip.
  rationale   two sentences, specific to THIS candidate's experience.
  resembles   which of their past applications this most resembles, or "" if none.
  concerns    the strongest honest reason NOT to apply, or "" if there is none.
  angle       the one thing they should lead with in the application.

Return ONLY a JSON array of objects with keys:
  ["id", "fit_score", "rationale", "resembles", "concerns", "angle"]

They previously applied to: %s

CANDIDATE PROFILE
=================
%s

POSTINGS
========
%s""" % (", ".join(_as_list(profile.get("applied_companies"))) or "nothing recorded",
         json.dumps(profile, indent=2)[:5000],
         json.dumps(listing, indent=2)[:20000])

    data = llm.ask_json(prompt, strong=True, system=SYSTEM)
    if isinstance(data, dict):
        data = data.get("rankings") or data.get("postings") or []
    scores: Dict[str, Dict[str, Any]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        posting_id = str(item.get("id") or "").strip()
        if not posting_id:
            continue
        try:
            score = int(float(item.get("fit_score") or 0))
        except (TypeError, ValueError):
            score = 0
        scores[posting_id] = {
            "fit_score": max(0, min(100, score)),
            "rationale": str(item.get("rationale") or "").strip(),
            "resembles": str(item.get("resembles") or "").strip(),
            "concerns": str(item.get("concerns") or "").strip(),
            "angle": str(item.get("angle") or "").strip(),
        }
    return scores
