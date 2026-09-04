"""Which URLs count as a real job posting.

The open web's job layer is mostly noise: scrapers that republish dead listings,
staffing mills that post phantom roles to harvest resumes, and SEO farms that
rank for every job title in every city. A tool that searches "jobs in
Albuquerque" and reports what it finds is a machine for generating false hope.

So jobscout only trusts postings that come from the employer:

* **applicant-tracking systems** — Greenhouse, Lever, Ashby, Workday and friends.
  A ``boards.greenhouse.io/acme/jobs/123`` link *is* Acme's own listing: the
  company pays for the board, and a closed role comes off it.
* **the employer's own domain** — ``careers.acme.com``, matched against the
  company name so a lookalike domain does not sneak through.
* **government, national-lab and university sites** — ``.gov``, ``.mil``,
  ``.edu``, which matters a lot if you are job-hunting in New Mexico.

Everything else is dropped, by host, with a reason you can read in the report.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

#: Applicant-tracking systems. A posting here is the employer's own listing.
#: Mapped host-suffix -> ATS name.
ATS_HOSTS: Dict[str, str] = {
    "boards.greenhouse.io": "Greenhouse",
    "job-boards.greenhouse.io": "Greenhouse",
    "greenhouse.io": "Greenhouse",
    "jobs.lever.co": "Lever",
    "lever.co": "Lever",
    "jobs.ashbyhq.com": "Ashby",
    "ashbyhq.com": "Ashby",
    "myworkdayjobs.com": "Workday",
    "wd1.myworkdaysite.com": "Workday",
    "wd5.myworkdaysite.com": "Workday",
    "workday.com": "Workday",
    "smartrecruiters.com": "SmartRecruiters",
    "jobs.smartrecruiters.com": "SmartRecruiters",
    "icims.com": "iCIMS",
    "taleo.net": "Taleo",
    "oraclecloud.com": "Oracle Recruiting",
    "apply.workable.com": "Workable",
    "workable.com": "Workable",
    "jobs.jobvite.com": "Jobvite",
    "jobvite.com": "Jobvite",
    "bamboohr.com": "BambooHR",
    "recruiting.paylocity.com": "Paylocity",
    "rippling.com": "Rippling",
    "jobs.polymer.co": "Polymer",
    "breezy.hr": "Breezy",
    "recruitee.com": "Recruitee",
    "teamtailor.com": "Teamtailor",
    "pinpointhq.com": "Pinpoint",
    "eightfold.ai": "Eightfold",
    "phenompeople.com": "Phenom",
    "avature.net": "Avature",
    "successfactors.com": "SuccessFactors",
    "brassring.com": "BrassRing",
    "silkroad.com": "SilkRoad",
    "applytojob.com": "JazzHR",
    "hire.trakstar.com": "Trakstar",
    "dover.com": "Dover",
    "getro.com": "Getro",
    "consider.com": "Consider",
}

#: Public-sector boards, which are authoritative for the roles they carry.
PUBLIC_HOSTS: Dict[str, str] = {
    "usajobs.gov": "USAJOBS",
    "governmentjobs.com": "NEOGOV (government)",
    "schooljobs.com": "NEOGOV (schools)",
    "interfolio.com": "Interfolio (academic)",
    "academicjobsonline.org": "AcademicJobsOnline",
}

#: Aggregators, scrapers, staffing mills and SEO farms. Never trusted, even when
#: the underlying job is real — the link rots, the date is wrong, and the
#: "apply" button often goes to a resume harvester rather than the employer.
DENY_HOSTS = (
    "indeed.com", "ziprecruiter.com", "glassdoor.com", "simplyhired.com",
    "monster.com", "careerbuilder.com", "dice.com", "talent.com", "jooble.org",
    "neuvoo.com", "adzuna.com", "lensa.com", "jobrapido.com", "trovit.com",
    "upwork.com", "freelancer.com", "flexjobs.com", "snagajob.com",
    "theladders.com", "beebee.com", "learn4good.com", "jobcase.com",
    "myjobhelper.com", "jobs2careers.com", "getwork.com", "joblist.com",
    "hiring.cafe", "jobright.ai", "startup.jobs", "himalayas.app",
    "wellfound.com", "angel.co", "builtin.com", "otta.com", "workatastartup.com",
    "remoterocketship.com", "weworkremotely.com", "remoteok.com", "remotive.com",
    "jobgether.com", "nodesk.co", "justremote.co", "workingnomads.com",
    "linkedin.com", "facebook.com", "x.com", "twitter.com", "reddit.com",
    "craigslist.org", "jobot.com", "roberthalf.com", "randstad.com",
    "aerotek.com", "teksystems.com", "insightglobal.com", "kforce.com",
    "apexsystems.com", "motionrecruitment.com", "cybercoders.com",
    "medium.com", "substack.com", "quora.com", "youtube.com",
)

#: Trusted public-sector / academic top-level domains.
PUBLIC_SUFFIXES = (".gov", ".mil", ".edu")

#: Words a legitimate careers URL almost always contains.
CAREER_PATH_WORDS = ("career", "job", "opening", "position", "vacan", "employment",
                     "opportunit", "join-us", "joinus", "workwithus", "hiring",
                     "recruit", "apply", "requisition", "req")

_TOKEN_NOISE = {
    "the", "inc", "llc", "ltd", "corp", "corporation", "company", "co", "group",
    "holdings", "national", "laboratory", "laboratories", "labs", "lab",
    "technologies", "technology", "systems", "solutions", "services", "and",
    "of", "for", "university", "institute", "center", "centre",
}

# Source classes, best first.
ATS = "ats"
EMPLOYER = "employer"
PUBLIC = "public"
UNKNOWN = "unknown"
DENIED = "denied"

TRUSTED = (ATS, EMPLOYER, PUBLIC)


def host_of(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = (urlsplit(url).netloc or "").lower()
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, candidate: str) -> bool:
    """Suffix match on domain-label boundaries, so ``notindeed.com`` is not Indeed."""
    return host == candidate or host.endswith("." + candidate)


def company_tokens(company: str) -> List[str]:
    words = re.split(r"[^a-z0-9]+", (company or "").lower())
    return [w for w in words if w and w not in _TOKEN_NOISE and len(w) > 2]


def is_employer_host(host: str, company: str) -> bool:
    """Does this host plausibly belong to the company itself?

    Requires a company word to appear in the *registrable* part of the domain,
    not just anywhere in the URL, so ``jobs.indeed.com/sandia`` does not pass as
    Sandia's own site.
    """
    if not host or not company:
        return False
    labels = host.split(".")
    core = ".".join(labels[-3:]) if len(labels) > 2 else host
    tokens = company_tokens(company)
    if not tokens:
        return False
    flat = re.sub(r"[^a-z0-9]", "", core)
    for token in tokens:
        if token in flat:
            return True
    # Acronyms: "Los Alamos National Laboratory" -> "lanl". Built from the FULL
    # name, since the acronym is exactly what the noise words contribute to, and
    # matched against whole domain labels so it cannot hit mid-word by accident.
    words = [w for w in re.split(r"[^a-z0-9]+", (company or "").lower())
             if w and w not in ("the", "of", "and", "for", "a")]
    acronym = "".join(w[0] for w in words)
    if len(acronym) >= 3 and acronym in labels:
        return True
    return False


def classify(url: str, company: str = "") -> Tuple[str, str]:
    """Classify a posting URL. Returns ``(source_class, human_label)``."""
    host = host_of(url)
    if not host:
        return DENIED, "no URL"
    for denied in DENY_HOSTS:
        if _host_matches(host, denied):
            return DENIED, "aggregator or staffing site (%s)" % denied
    for ats_host, name in ATS_HOSTS.items():
        if _host_matches(host, ats_host):
            return ATS, name
    for public_host, name in PUBLIC_HOSTS.items():
        if _host_matches(host, public_host):
            return PUBLIC, name
    if host.endswith(PUBLIC_SUFFIXES):
        return PUBLIC, "government/academic site (%s)" % host
    if is_employer_host(host, company):
        return EMPLOYER, "employer site (%s)" % host
    return UNKNOWN, "unrecognised site (%s)" % host


def check_source(url: str, company: str = "") -> Tuple[bool, str, str]:
    """The hard source gate: ``(accepted, source_class, reason)``."""
    source_class, label = classify(url, company)
    if source_class in TRUSTED:
        return True, source_class, label
    if source_class == DENIED:
        return False, source_class, label
    return False, source_class, (
        "%s — not the employer's own board, so the listing cannot be trusted" % label)


#: ATS boards whose first path segment is the employer's handle.
SLUG_ATS_HOSTS = ("jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io",
                  "jobs.ashbyhq.com", "apply.workable.com", "breezy.hr",
                  "recruitee.com", "teamtailor.com")

_SLUG_TLD = re.compile(r"\.(com|io|ai|co|net|org|dev|xyz|us|bio|tech)$", re.I)


def clean_board_url(url: str) -> str:
    """Repair the one mistake models reliably make with ATS board URLs.

    Asked for a company's Lever board they will often paste the company's
    *domain* in as the handle — ``jobs.lever.co/descarteslabs.com`` — which 404s.
    The handle is a slug, never a hostname, so a trailing TLD on that first path
    segment is always wrong and is dropped.
    """
    if not url:
        return url
    host = host_of(url)
    if not any(_host_matches(host, ats) for ats in SLUG_ATS_HOSTS):
        return url
    parts = urlsplit(url if "://" in url else "https://" + url)
    segments = [seg for seg in (parts.path or "").split("/") if seg]
    if not segments:
        return url
    cleaned = _SLUG_TLD.sub("", segments[0])
    if cleaned == segments[0] or not cleaned:
        return url
    segments[0] = cleaned
    return "%s://%s/%s" % (parts.scheme or "https", parts.netloc, "/".join(segments))


def looks_like_careers_page(url: str) -> bool:
    lowered = (url or "").lower()
    if classify(url)[0] == ATS:
        return True
    return any(word in lowered for word in CAREER_PATH_WORDS)


def ats_search_hints() -> List[str]:
    """Search operators handed to the discovery agent, so it looks in good places."""
    return [
        "site:boards.greenhouse.io", "site:job-boards.greenhouse.io",
        "site:jobs.lever.co", "site:jobs.ashbyhq.com",
        "site:myworkdayjobs.com", "site:jobs.smartrecruiters.com",
        "site:icims.com", "site:apply.workable.com", "site:usajobs.gov",
    ]


def trusted_source_summary() -> str:
    """A sentence for prompts and the README."""
    return ("applicant-tracking boards (Greenhouse, Lever, Ashby, Workday, "
            "SmartRecruiters, iCIMS, Workable, Jobvite), the employer's own "
            "careers domain, and .gov/.mil/.edu sites")
