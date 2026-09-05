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
    """Describe the CONFIGURED policy.

    Every place named here comes from the user's settings. Naming a specific
    city or state anywhere in this module would steer the model toward it no
    matter whose policy was loaded — which is the whole thing this tool is
    supposed to get right. ``tests/test_privacy.py`` enforces that.
    """
    lines = ["HARD LOCATION RULE (non-negotiable — a role that fails this is worthless):"]
    if policy.allowed_states:
        names = ", ".join(policy.allowed_states)
        lines.append("  * Onsite or hybrid roles are acceptable ONLY in: %s." % names)
    if policy.allowed_cities:
        lines.append("  * Specifically including: %s."
                     % ", ".join(sorted(set(policy.allowed_cities))))
    if policy.allow_remote:
        lines.append("  * Fully remote roles are acceptable, BUT many 'remote' roles are "
                     "silently fenced to a region the candidate does not live in "
                     "('remote — must reside in <somewhere else>'). Those are NOT "
                     "acceptable. Always report the posting's exact stated location "
                     "text, verbatim, so the fence can be checked.")
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

#: Five ways of looking for an employer. Asking one general question five times
#: returns the same famous names five times; asking five different questions
#: covers the map — and they run at once, so breadth costs no extra waiting.
#: Each angle looks for a different KIND of employer, and says whether the
#: candidate's geography is a constraint on it AT ALL.
#:
#: It used to be pinned to all five. The full location rule — "non-negotiable",
#: "a role that fails this is worthless", with the home cities named — was
#: prepended to every angle including the one whose own text says geography
#: should not narrow it. So every angle drifted local, and the result was the
#: worst of both: an employer list crowded with nearby institutions whose
#: boards could not be read, and a shortlist that ended up 85% remote anyway
#: because the remote startup boards were the only ones that answered.
#:
#: Where an employer sits is a hard filter on POSTINGS, applied deterministically
#: in filters.py, which is where it belongs. It is not a reason to think about
#: fewer companies. Local is worth having only when local is good.
SEARCH_ANGLES = (
    ("Large institutions PHYSICALLY located in the accepted area: national labs, "
     "federal facilities, universities, hospitals, utilities, state agencies, and "
     "the defence and engineering contractors that cluster around them.", True),

    ("Remote-first companies ANYWHERE IN THE COUNTRY whose core product is in "
     "this candidate's strongest technical domain. Geography is irrelevant here "
     "and must not narrow the list: where they are headquartered does not "
     "matter, only that they hire remotely.", False),

    ("Companies whose actual product or research matches the candidate's "
     "differentiators — the unusual combination on their CV, not the generic "
     "part. Smaller and less famous is fine, and often better. Judge them on "
     "the match alone, wherever they are.", False),

    ("Adjacent industries that hire this background without advertising for it "
     "by name: sectors where the same skills solve a differently-worded "
     "problem.", False),

    ("Startups and scale-ups funded in the last few years in this candidate's "
     "domains, remote-friendly or in the accepted area. Favour the ones that "
     "are hard to find: recently funded, still small, no careers-page SEO. "
     "These are the employers a candidate cannot reach by browsing a big job "
     "site, which is the whole reason to look this way.", False),

    # Aggregators are never trusted as a source of POSTINGS — the links rot and
    # the dates lie, which is why sources.py refuses them. As a way of learning
    # WHICH EMPLOYERS EXIST they are perfectly good, and they surface exactly
    # the small companies a curated list of famous names never will. The name
    # is all that is taken; the board is then found and read at the source.
    ("Employers you can see hiring on startup job boards, funding announcements, "
     "accelerator and VC portfolio pages, and industry-specific job boards. Use "
     "these ONLY to learn which companies exist and are hiring — return the "
     "EMPLOYER NAME, never a link to the aggregator. Prefer companies that do "
     "not appear on lists of well-known employers.", False),
)


def propose_companies(llm: LLM, profile: Dict[str, Any], policy: LocationPolicy,
                      known: Sequence[str], count: int = 25,
                      angle: str = "", geographic: bool = True) -> List[Company]:
    """Name employers who could plausibly hire this candidate.

    This is the step that makes the tool work: instead of trawling the open web
    for postings, it decides *who to ask*, and later stages read those employers'
    own boards. It is also where quality is actually decided — everything after
    it can only filter what this returns.

    ``geographic`` says whether the candidate's location constrains THIS angle.
    Local institutions, yes. Remote-first employers, emphatically not.
    """
    known_block = ", ".join(known) if known else "none yet"
    # Only a geography-bound angle gets the location rule. On the others it is
    # actively wrong: it narrowed a nationwide remote search down to the
    # candidate's own state, which is not where remote employers are.
    if geographic:
        where = _policy_block(policy)
        opening = ("propose %d SPECIFIC, REAL employers who could plausibly hire "
                   "this person, and whose jobs would satisfy the location rule.")
    else:
        where = ("LOCATION: not a constraint on this list. The candidate does not "
                 "relocate, so a role must eventually be remote or in their area, "
                 "but that is checked later against the actual posting. Do NOT "
                 "narrow this list to employers near them — propose the best "
                 "employers for this background wherever they are, as long as "
                 "they hire remotely.")
        opening = ("propose %d SPECIFIC, REAL employers who could plausibly hire "
                   "this person.")
    prompt = ("""Given the candidate profile below, """ + opening + """

%s

WHERE TO LOOK THIS TIME — stay inside this brief, it is one of several being
run in parallel and overlapping with the others wastes the search:
%s

Use web search to ground your list in reality.

Rules:
  * Real organisations only, with a name you have seen on the web. No invented names.
  * Do NOT repeat any of these, which are already on the list: %s
  * Prefer employers who are plausibly hiring NOW over famous names who are not.
  * Spread the list: do not return 20 national labs or 20 AI startups.
  * QUALITY OVER QUANTITY. %d is a ceiling, not a quota. A shorter list of
    employers you would genuinely argue for is worth more than a padded one,
    and a weak name here costs a board lookup and a scan for nothing. If you
    can only make a real case for six, return six.
  * For each, say concretely WHY this candidate fits — reference their actual
    skills or domains, not generic praise.

Return ONLY a JSON array:

[
  {
    "name": "exact legal or common name of the employer",
    "why": "one or two sentences tying THIS candidate's background to THIS employer",
    "presence": "where they are — name the actual city/state, or say they are remote-first",
    "hiring_signal": "any evidence you saw that they are hiring, or '' if none"
  }
]

CANDIDATE PROFILE
=================
%s""") % (count, where,
          angle or "Any employer who fits this candidate.",
          known_block, count, json.dumps(profile, indent=2)[:6000])

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

STRONGLY PREFER AN APPLICANT-TRACKING BOARD over the company's own careers page.
Boards on Greenhouse, Lever, Ashby, SmartRecruiters and Workable can be read
directly through a public API, which is complete and exact; a marketing careers
page is usually a JavaScript shell that reads as empty. So:

  1. BEST — the ATS board itself:
       boards.greenhouse.io/<slug>        job-boards.greenhouse.io/<slug>
       jobs.lever.co/<slug>               jobs.ashbyhq.com/<slug>
       jobs.smartrecruiters.com/<slug>    apply.workable.com/<slug>
     A page like acme.com/careers very often EMBEDS one of these. Fetch the
     careers page and look for the board behind it — an iframe, a link, or an
     "apply" button pointing at one of those hosts. Follow it and give me THAT
     URL. The <slug> is a short handle ("descarteslabs"), never a domain
     ("descarteslabs.com").
  2. Next best — a Workday or iCIMS board: <slug>.myworkdayjobs.com/...,
     <slug>.icims.com
  3. Otherwise — the careers section on their own .gov/.edu/company site.

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

METHOD: actually fetch the board.
  * Big employers list thousands of roles. USE THE BOARD'S OWN FILTERS rather
    than paging through everything: most boards take a location and a keyword in
    the query string (e.g. ?q=data+engineer&locations=New+Mexico, or
    ?location=Remote). Search the target titles, and filter to the accepted
    locations and to remote, before you read results.
  * If it paginates, fetch the next pages too, but stop once you have covered the
    filtered results.
  * Return at most 15 roles. If more match, keep the best fits.
  * For each role, give the direct URL to that specific posting on that same site.

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

#: Roles per ranking call. The listing used to be pasted into one prompt and
#: truncated at 20k characters, which quietly dropped everything past the cut —
#: and a JSON array sliced mid-object often failed to parse at all, losing the
#: scores for the whole run.
RANK_BATCH = 20


def rank_postings(llm: LLM, postings: Sequence[Posting],
                  profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Score the survivors against the candidate's actual background.

    Batched, because a big result set is exactly when the scores matter most.
    """
    postings = list(postings)
    if not postings:
        return {}
    if len(postings) > RANK_BATCH:
        from concurrent.futures import ThreadPoolExecutor

        batches = [postings[i:i + RANK_BATCH]
                   for i in range(0, len(postings), RANK_BATCH)]
        merged: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
            for scores in pool.map(lambda b: _rank_batch(llm, b, profile), batches):
                merged.update(scores)
        return merged
    return _rank_batch(llm, postings, profile)


def _rank_batch(llm: LLM, postings: Sequence[Posting],
                profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    listing = [
        {"id": p.id, "company": p.company, "title": p.title, "location": p.location,
         "posted": p.posted, "salary": p.salary, "summary": p.summary}
        for p in postings
    ]
    prompt = """Score each of these verified, in-location job postings against the candidate.

Be honest and discriminating. If a role is a weak fit, score it low and say why —
the candidate is deciding where to spend real hours writing an application, and a
list where everything is a 9 is useless to them.

Score TWO different things for each posting. They are not the same, and
conflating them is the mistake that makes job tools useless:

  fit_score   0-100: how well their background matches what the role ASKS FOR.
  likelihood  0-100: their realistic chance of actually GETTING it. This is a
              different question. Weigh how many people will apply, how senior
              the role is relative to them, whether they meet hard gates
              (citizenship, clearance, licence, degree), whether they are local
              to a role that prefers local candidates, whether they have a warm
              signal (a past application, a recruiter thread, an alumni or
              former-employer connection visible in the profile), and how
              specific the requirements are. A perfect-fit role at a famous
              employer with 800 applicants can be a 90 fit and a 15 likelihood.
              A merely-good role where they clear a gate most applicants do not
              can be a 65 fit and a 70 likelihood. Be realistic, not kind.

Also return:
  rationale   two sentences on the FIT, specific to THIS candidate's experience.
  odds        one sentence on the LIKELIHOOD — what helps or hurts their odds here.
  resembles   which of their past applications this most resembles, or "" if none.
  concerns    the strongest honest reason NOT to apply, or "" if there is none.
  angle       the one thing they should lead with in the application.

Return ONLY a JSON array of objects with keys:
  ["id", "fit_score", "likelihood", "rationale", "odds", "resembles", "concerns", "angle"]

They previously applied to: %s

CANDIDATE PROFILE
=================
%s

POSTINGS
========
%s""" % (", ".join(_as_list(profile.get("applied_companies"))) or "nothing recorded",
         json.dumps(profile, indent=2)[:5000],
         json.dumps(listing, indent=2))

    try:
        data = llm.ask_json(prompt, strong=True, system=SYSTEM)
    except LLMError:
        return {}      # one bad batch must not cost the whole run its scores
    if isinstance(data, dict):
        data = data.get("rankings") or data.get("postings") or []
    scores: Dict[str, Dict[str, Any]] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        posting_id = str(item.get("id") or "").strip()
        if not posting_id:
            continue
        def _score(key):
            try:
                return max(0, min(100, int(float(item.get(key) or 0))))
            except (TypeError, ValueError):
                return 0

        scores[posting_id] = {
            "fit_score": _score("fit_score"),
            "likelihood": _score("likelihood"),
            "rationale": str(item.get("rationale") or "").strip(),
            "odds": str(item.get("odds") or "").strip(),
            "resembles": str(item.get("resembles") or "").strip(),
            "concerns": str(item.get("concerns") or "").strip(),
            "angle": str(item.get("angle") or "").strip(),
        }
    return scores
