"""Your job search, reconstructed from your own mailbox.

A search that has run long enough stops being a plan and becomes a pile of
half-remembered threads. You applied somewhere in December and never heard
back — or did you? A recruiter was excited about you in July and then the role
was filled, but she still works that exact market. Somebody at a company you
are chasing already interviewed you two years ago and you cannot remember his
name. All of it is sitting in your mail, and none of it is anywhere you can
sort it.

This module turns that pile into three lists:

* **Applications** — every employer you actually applied to, with the date and
  how it ended. Applicant tracking systems send a receipt for every submission,
  so this history is complete in a way your memory is not.
* **Inbound recruiters** — everyone who approached *you*. These are the warmest
  contacts in any job search: they came to you, which means they already
  decided you were a fit, and the ones who work your niche will have another
  role next quarter whatever happened to this one.
* **Named humans** — the people inside those companies, harvested from
  interview scheduling mail, which is the one kind of automated message that
  names actual staff.

Nothing here talks to a mail server. It reads a list of message records that
some ingester — a Gmail connector, an IMAP script, an exported mbox — has
already dumped to disk. That keeps the parsing testable offline, keeps the
mailbox itself out of this repository, and means the tool is not married to one
mail provider.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .corpus import normalize_company
from .models import parse_date

# --- what a message can be -------------------------------------------------

RECRUITER = "recruiter"          # a human approached you about a role
APPLICATION = "application"      # an ATS acknowledged something you sent
REJECTION = "rejection"          # ... and later declined you
INTERVIEW = "interview"          # a real conversation was scheduled
ALERT = "alert"                  # algorithmic job spam, no human behind it
OTHER = "other"

#: Applicant tracking systems, by the domain their mail comes from. The value
#: is how the employer's own name can be recovered: "tenant" means the local
#: part of the sender address is the employer (``amgen@myworkday.com``), and
#: "body" means it is multi-tenant and the name has to be read out of the text.
ATS_SENDERS = {
    "myworkday.com": "tenant",
    "icims.com": "tenant",
    "ashbyhq.com": "body",
    "hire.lever.co": "body",
    "lever.co": "body",
    "greenhouse.io": "body",
    "smartrecruiters.com": "body",
    "workable.com": "body",
    "breezy.hr": "body",
    "jobvite.com": "body",
    "taleo.net": "body",
    "successfactors.com": "body",
}

#: Mail that comes from a person via a platform's relay. The sender header is
#: useless (it is always the relay), so the human's name lives in the body.
RELAY_SENDERS = ("hit-reply@linkedin.com", "inmail-hit-reply@linkedin.com")

#: Algorithmic job spam. Distinguishing this from a real approach matters: a
#: list padded with robot mail is a list nobody reads twice.
ALERT_SENDERS = ("jobs-noreply@linkedin.com", "jobalerts-noreply@linkedin.com",
                 "notifications-noreply@linkedin.com", "job-alerts@",
                 "noreply@indeed.com", "alerts@", "jobalerts@")

_APPLIED = re.compile(
    r"(thank(s| you) for (applying|your application|your interest)|"
    r"we (just )?received your application|your application (has been |was )?receiv|"
    r"thanks for applying|application (was )?submitted|"
    r"we appreciate your interest)", re.I)

_REJECTED = re.compile(
    r"(we(?:'ve| have)? ?(?:made the )?deci(?:de|ded|sion)|not (be )?(moving|move) forward|"
    r"will not be (moving|progress)|unfortunately[^.]{0,60}(not|unable|other candidate)|"
    r"pursu\w+ other candidates|position has been filled|role (has been|is) filled|"
    r"no longer (available|open|accepting)|not (to )?(be )?select|"
    r"we (did not|didn't) (select|move))", re.I)

_INTERVIEW = re.compile(
    r"(upcoming interview|your (confirmed )?interview|interview (confirmed|scheduled|reminder)|"
    r"recruiter screen|phone screen|schedule (a|your) (call|chat|interview)|"
    r"invitation to interview|next (step|round))", re.I)

#: Someone selling you a role. Kept broad on purpose — a recruiter's opening
#: line has a thousand forms, and the sender check has already done the work of
#: excluding robots.
_PITCH = re.compile(
    r"(opportunit|hiring for|we('| a)re (growing|hiring|looking)|"
    r"i('m| am) (a |an )?(recruit|talent|technical recruit|working with|supporting|reaching)|"
    r"came across your (profile|background)|saw your (profile|work|background)|"
    r"are you (on the job market|open to|interested)|reaching out|"
    r"role (at|with)|position (at|with)|your background)", re.I)

#: The employer's name in ATS boilerplate. Ordered most specific first: an
#: early loose pattern would swallow the sentence a later one gets right.
#: Where a company name stops. Punctuation ends it, and so does the start of
#: the next clause — "applying to Kestrel Bio and we will review your
#: application" has no punctuation until a point well past the name.
_END = r"(?:[!.,\n]|\s+(?:and|we|our|for|to|as)\b)"

_COMPANY_PATTERNS = tuple(re.compile(pattern % _END, flags) for pattern, flags in (
    (r"(?:role|position) (?:at|with) ([A-Z][\w&.'’\- ]{1,48}?)%s", re.I),
    (r"thank(?:s| you) for applying (?:to|for) ([A-Z][\w&.'’\- ]{1,48}?)%s", re.I),
    (r"your (?:recent )?interest in (?:working (?:with|at) )?(?:us at )?"
     r"([A-Z][\w&.'’\- ]{1,48}?)%s", re.I),
    (r"joining (?:the team at |our team at |)([A-Z][\w&.'’\- ]{1,48}?)%s", 0),
    (r"interview with ([A-Z][\w&.'’\- ]{1,48}?)%s", 0),
    (r"application (?:to|for) ([A-Z][\w&.'’\- ]{1,48}?)%s", 0),
))

#: How a recruiter introduces themselves.
_NAME_PATTERNS = (
    re.compile(r"[Mm]y name is ([A-Z][a-z’'\-]+(?: [A-Z][a-z’'\-]+){0,2})"),
    re.compile(r"^(?:hi|hello|hey)[^,\n]{0,40},?\s*(?:my name is )?"
               r"([A-Z][a-z’'\-]+(?: [A-Z][a-z’'\-]+){0,2})[,.]", re.M),
    re.compile(r"(?:regards|thanks|best|sincerely|cheers)[,!]?\s*\n+\s*"
               r"([A-Z][a-z’'\-]+(?: [A-Z][a-z’'\-]+){0,2})\s*$", re.M | re.I),
)

#: LinkedIn's relay writes every message to the same shape: the subject twice,
#: then the sender's name alone on a line, then a "Reply" link. That structure
#: is worth more than any amount of guessing at prose, because it holds even
#: when the recruiter never introduces themselves — which most of them don't.
_RELAY_HEADER = re.compile(
    r"^\s*([A-Z][\w’'\-]+(?:\s+[A-Z][\w’'\-]+){0,2})"
    r"(?:,\s*[A-Za-z.]{2,8})?\s*\n\s*Reply\s*$", re.M)

#: Letters after a name are a credential, not part of it.
_CREDENTIALS = re.compile(r"\s+(?:MSc|MS|MBA|PhD|PHR|SHRM|CPC|BSc|MA)\.?$", re.I)

#: The employer behind an approach. Tried before the applicant-tracking
#: patterns for recruiter mail, because "a key need we have here at SandboxAQ"
#: names the employer and no ATS phrasing will ever match it.
_HIRING_PATTERNS = (
    re.compile(r"(?:here|we|role|position|opening)\s+(?:at|with)\s+"
               r"([A-Z][\w&.'’\- ]{1,40}?)(?:[.,!\n]|\s+(?:and|within|in|for|to)\b)"),
    re.compile(r"recruiting\s+(?:an?|the)?\s?[\w ]{0,40}?\bfor\s+"
               r"([A-Z][\w&.'’\- ]{1,40}?)(?:[.,!\n])"),
    re.compile(r"hiring\s+for\s+([A-Z][\w&.'’\- ]{1,40}?)(?:[.,!\n])"),
    re.compile(r"(?:Talent Acquisition|Recruit\w*|Engineering)\s+at\s+"
               r"([A-Z][\w&.'’\- ]{1,40}?)(?:[.,!\n|]|$)", re.M),
)

#: Who the recruiter works for. An agency name is not the hiring company, but
#: it is the thing that makes them worth keeping: an agency that placed one
#: cheminformatics role will have the next one too.
_AGENCY_PATTERNS = (
    re.compile(r"recruit\w*\s+(?:with|at|for)\s+([A-Z][\w&.'’\- ]{1,40}?)"
               r"(?:[.,!\n]|\s+(?:and|who|working))"),
    re.compile(r"i(?:'m| am)\s+(?:currently\s+)?(?:with|at)\s+([A-Z][\w&.'’\- ]{1,40}?)"
               r"(?:[.,!\n])"),
    re.compile(r"on behalf of\s+(?:our client\s+)?([A-Z][\w&.'’\- ]{1,40}?)(?:[.,!\n])"),
)

#: Words that look like a name to a regex but are not one.
_NOT_A_NAME = {"the", "there", "this", "that", "hope", "just", "thanks", "thank",
               "please", "would", "could", "unfortunately", "apologies", "sorry",
               "looking", "great", "perfect", "absolutely", "hi", "hello", "hey",
               "your", "our", "we", "i", "you", "team", "role", "position", "best"}

#: Words that mean the phrase is a job title, not an employer.
_LOOKS_LIKE_A_TITLE = re.compile(
    r"\b(engineer|scientist|developer|manager|director|analyst|architect|"
    r"specialist|lead|intern|associate|consultant|designer|researcher|"
    r"technician|administrator|officer|president|position|role)\b", re.I)

#: Subject-line noise LinkedIn adds when a thread continues.
_SUBJECT_NOISE = re.compile(r"^\s*((re|fwd?|message replied|new message)\s*:\s*)+", re.I)


@dataclass
class Message:
    """One email, in the shape every ingester must produce.

    Deliberately minimal: an id to deduplicate on, enough headers to know who
    and when, and whatever text was available. ``body`` may be empty — a
    listing-only fetch gives just a snippet, and everything here degrades to
    working off the subject and snippet alone.
    """
    id: str = ""
    thread_id: str = ""
    date: str = ""
    sender: str = ""
    to: str = ""
    subject: str = ""
    snippet: str = ""
    body: str = ""
    from_me: bool = False

    @property
    def when(self) -> Optional[dt.date]:
        return parse_date((self.date or "")[:10])

    @property
    def domain(self) -> str:
        match = re.search(r"@([\w.\-]+)", self.sender or "")
        return (match.group(1) if match else "").lower()

    @property
    def counterpart(self) -> str:
        """The other person's address, whichever direction this went."""
        return ((self.to if self.from_me else self.sender) or "").strip().lower()

    @property
    def text(self) -> str:
        return "\n".join(p for p in (self.subject, self.snippet, self.body) if p)

    @property
    def clean_subject(self) -> str:
        return _SUBJECT_NOISE.sub("", self.subject or "").strip()


def classify(message: Message) -> str:
    """What kind of message is this.

    Order matters and encodes a judgment: a rejection is a rejection even when
    it arrives wrapped in interview pleasantries, and anything from a robot
    sender is an alert no matter how personal the template pretends to be.
    """
    sender = (message.sender or "").lower()
    if any(marker in sender for marker in ALERT_SENDERS):
        return ALERT
    text = message.text
    if _REJECTED.search(text):
        return REJECTION
    if _INTERVIEW.search(text):
        return INTERVIEW
    if _ats_kind(message) and _APPLIED.search(text):
        return APPLICATION
    if "inmail-hit-reply@" in sender and not message.from_me:
        return RECRUITER
    if any(relay in sender for relay in RELAY_SENDERS) and not message.from_me:
        return RECRUITER if _PITCH.search(text) else OTHER
    if _ats_kind(message):
        return APPLICATION
    if _PITCH.search(text) and not message.from_me:
        return RECRUITER
    return OTHER


def _ats_kind(message: Message) -> str:
    domain = message.domain
    for known, kind in ATS_SENDERS.items():
        if domain == known or domain.endswith("." + known):
            return kind
    return ""


def extract_company(message: Message) -> str:
    """Recover the employer's name.

    Workday and iCIMS give each customer their own sending address, so
    ``amgen@myworkday.com`` names the employer for free. Everyone else is
    multi-tenant and the name has to be read out of the boilerplate.
    """
    if classify(message) == RECRUITER:
        for pattern in _HIRING_PATTERNS:
            match = pattern.search(_unwrap(message.body or message.snippet or ""))
            if match:
                name = _tidy_company(match.group(1))
                if name:
                    return name
    for pattern in _COMPANY_PATTERNS:
        match = pattern.search(message.text)
        if match:
            name = _tidy_company(match.group(1))
            if name:
                return name
    if _ats_kind(message) == "tenant":
        local = (message.sender or "").split("@")[0]
        local = re.sub(r"[._\-]?(no-?reply|donotreply|careers|jobs|talent|hr)$", "",
                       local, flags=re.I)
        if local and not re.fullmatch(r"(no-?reply|donotreply|info|mail)", local, re.I):
            return local.replace("_", " ").replace("-", " ").strip().title()
    return ""


def _tidy_company(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip(" \t\n,.!-—"))
    # Boilerplate runs on past the name; cut at the first clause break.
    name = re.split(r"\s+(?:and|for|to|as|we|our|role|position|team)\b", name, 1, re.I)[0]
    name = name.strip(" ,.!-")
    if len(name) < 2 or len(name) > 48:
        return ""
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip()
    if name.lower() in _NOT_A_NAME:
        return ""
    # "your interest in the Software Engineer, Platform role" reads exactly like
    # a company name to a regex. Job titles are the one thing it must not be.
    if _LOOKS_LIKE_A_TITLE.search(name):
        return ""
    return name


def extract_person(message: Message) -> str:
    """The human's name.

    The relay's own layout is tried first and is nearly always right; the prose
    patterns are the fallback for mail that arrived some other way.
    """
    body = message.body or ""
    match = _RELAY_HEADER.search(body)
    if match:
        candidate = match.group(1).strip()
        candidate = _CREDENTIALS.sub("", candidate).strip()
        if candidate.split()[0].lower() not in _NOT_A_NAME and len(candidate) > 2:
            return candidate
    for pattern in _NAME_PATTERNS:
        match = pattern.search(message.body or message.snippet or "")
        if match:
            candidate = match.group(1).strip()
            first = candidate.split()[0].lower()
            if first in _NOT_A_NAME or len(candidate) < 3:
                continue
            return candidate
    return ""


def _unwrap(text: str) -> str:
    """Join hard-wrapped lines inside a paragraph.

    Mail clients wrap at 72 columns, which lands a line break in the middle of
    "Insight Global" often enough to matter. Blank lines still separate
    paragraphs, so the relay's own layout is untouched.
    """
    return re.sub(r"(?<!\n)\n(?!\n)", " ", text or "")


def extract_agency(message: Message) -> str:
    """The staffing firm behind an approach, when one is named."""
    for pattern in _AGENCY_PATTERNS:
        match = pattern.search(_unwrap(message.body or message.snippet or ""))
        if match:
            name = _tidy_company(match.group(1))
            if name:
                return name
    return ""


def extract_role(message: Message) -> str:
    """The job title being discussed, from the subject where possible."""
    subject = message.clean_subject
    for pattern in (
        re.compile(r"(?:opportunit\w*|opening|vacancy) for (?:an?|the )?"
                   r"([\w/&,\-\(\) ]{4,60}?)(?: role| position|$)", re.I),
        re.compile(r"(?:application|apply(?:ing)? (?:to|for)|interest in) (?:the )?"
                   r"([\w/&,\-\(\) ]{4,60}?) (?:role|position)", re.I),
        re.compile(r"^([\w/&,\-\(\) ]{4,60}?) (?:role|position|opportunit)", re.I),
        re.compile(r"\|\s*([\w/&,\-\(\) ]{4,60}?)\s*(?:\||$)"),
        re.compile(r"(?:hiring|recruiting) for (?:an?|the )?([\w/&,\-\(\) ]{4,60})", re.I),
    ):
        match = pattern.search(subject)
        if match:
            role = re.sub(r"\s+", " ", match.group(1)).strip(" -,|")
            if 3 < len(role) < 60:
                return role
    for pattern in (
        re.compile(r"(?:application|apply(?:ing)?|interest in) (?:for |to )?(?:the )?"
                   r"([\w/&,\-\(\) ]{4,60}?) (?:role|position)", re.I),
        re.compile(r"for the ([\w/&,\-\(\) ]{4,60}?) (?:role|position)", re.I),
    ):
        match = pattern.search(message.text)
        if match:
            role = re.sub(r"\s+", " ", match.group(1)).strip(" -,|")
            if 3 < len(role) < 60 and _LOOKS_LIKE_A_TITLE.search(role):
                return role
    return ""


# --- the three lists -------------------------------------------------------

@dataclass
class Application:
    """Somewhere you actually applied, and what became of it."""
    company: str = ""
    role: str = ""
    applied: str = ""
    last_heard: str = ""
    status: str = "no answer"      # or "rejected", "interviewed"
    ats: str = ""
    thread_id: str = ""

    @property
    def key(self) -> str:
        return normalize_company(self.company)


@dataclass
class Outreach:
    """A recruiter who came to you.

    ``replied`` is the field that matters most. An approach you never answered
    is a lead you still own; one you answered and let die is a relationship,
    which is a different message and usually a better one.
    """
    person: str = ""
    company: str = ""
    agency: str = ""
    role: str = ""
    said_about_place: str = ""
    first_contact: str = ""
    last_contact: str = ""
    replied: bool = False
    outcome: str = ""
    messages: int = 0
    thread_id: str = ""

    def dormant_days(self, today: Optional[dt.date] = None) -> Optional[int]:
        last = parse_date(self.last_contact)
        if not last:
            return None
        return ((today or dt.date.today()) - last).days


@dataclass
class Contact:
    """A named human, and where you know them from."""
    name: str = ""
    company: str = ""
    context: str = ""
    seen: str = ""

    @property
    def key(self) -> str:
        return "%s|%s" % (self.name.lower(), normalize_company(self.company))


def _threads(messages: Sequence[Message]) -> Dict[str, List[Message]]:
    grouped: Dict[str, List[Message]] = {}
    for message in messages:
        grouped.setdefault(message.thread_id or message.id, []).append(message)
    for thread in grouped.values():
        thread.sort(key=lambda m: m.date or "")
    return grouped


def applications(messages: Sequence[Message]) -> List[Application]:
    """Every employer you applied to, folded across the whole thread.

    Employers send several receipts for one submission and Workday sends the
    same mail twice, so this keys on the employer rather than the message and
    lets later mail in the thread upgrade the status.
    """
    found: Dict[str, Application] = {}
    for thread in _threads(messages).values():
        # Resolve the employer once for the whole thread. The later mail in a
        # thread is where the outcome lives, and it is exactly the mail that
        # stops naming the company — a rejection often says nothing but "after
        # reviewing, we will not be moving forward". Reading each message in
        # isolation therefore loses precisely the messages that matter.
        thread_company = next((extract_company(m) for m in thread
                               if extract_company(m)), "")
        for message in thread:
            kind = classify(message)
            if kind not in (APPLICATION, REJECTION, INTERVIEW):
                continue
            if not _ats_kind(message) and kind != REJECTION:
                continue
            company = extract_company(message) or thread_company
            if not company:
                continue
            key = normalize_company(company)
            entry = found.get(key)
            if entry is None:
                entry = Application(company=company, ats=message.domain,
                                    thread_id=message.thread_id)
                found[key] = entry
            when = message.date[:10]
            if kind == APPLICATION and (not entry.applied or when < entry.applied):
                entry.applied = when
            if when > (entry.last_heard or ""):
                entry.last_heard = when
            if not entry.role:
                entry.role = extract_role(message)
            # Rejection is terminal; an interview outranks silence but not a no.
            if kind == REJECTION:
                entry.status = "rejected"
            elif kind == INTERVIEW and entry.status != "rejected":
                entry.status = "interviewed"
    return sorted(found.values(), key=lambda a: a.applied or a.last_heard, reverse=True)


def outreach(messages: Sequence[Message]) -> List[Outreach]:
    """Everyone who approached you, one entry per conversation."""
    results: List[Outreach] = []
    for thread_id, thread in _threads(messages).items():
        inbound = [m for m in thread if not m.from_me and classify(m) == RECRUITER]
        if not inbound:
            continue
        first = inbound[0]
        entry = Outreach(
            person=next((extract_person(m) for m in inbound if extract_person(m)), ""),
            company=next((extract_company(m) for m in inbound if extract_company(m)), ""),
            agency=next((extract_agency(m) for m in inbound if extract_agency(m)), ""),
            role=extract_role(first),
            first_contact=first.date[:10],
            last_contact=max(m.date[:10] for m in thread if m.date),
            replied=any(m.from_me for m in thread) or len(thread) > 1,
            messages=len(thread),
            thread_id=thread_id,
            said_about_place=" ".join((m.body or m.snippet or "") for m in inbound)[:2000],
        )
        for message in thread:
            if _REJECTED.search(message.text):
                entry.outcome = "closed"
                break
        else:
            if any(classify(m) == INTERVIEW for m in thread):
                entry.outcome = "spoke"
        results.append(entry)
    results.sort(key=lambda o: o.last_contact, reverse=True)
    return results


def contacts(messages: Sequence[Message]) -> List[Contact]:
    """Named humans, from anywhere in the mailbox that names one."""
    found: Dict[str, Contact] = {}
    for message in messages:
        if message.from_me:
            continue
        name = extract_person(message)
        if not name:
            continue
        company = extract_company(message)
        kind = classify(message)
        contact = Contact(name=name, company=company,
                          context={RECRUITER: "recruiter who approached you",
                                   INTERVIEW: "interviewed you",
                                   REJECTION: "handled your application"}.get(kind, "emailed you"),
                          seen=message.date[:10])
        existing = found.get(contact.key)
        if existing is None or contact.seen > existing.seen:
            found[contact.key] = contact
    return sorted(found.values(), key=lambda c: c.seen, reverse=True)


# --- who you are actually talking to ---------------------------------------

#: Mail hosts that say nothing about where someone works.
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "live.com",
    "msn.com", "comcast.net", "mac.com",
}

#: Relays that stand in for a person rather than being one.
NOREPLY = re.compile(r"^(no-?reply|donotreply|notifications?|mailer|bounce|"
                     r"support|info|alerts?|jobs-noreply|.*-noreply)@", re.I)


@dataclass
class Correspondent:
    """A real human you have exchanged mail with.

    Keyed on the address rather than the name, because the address survives a
    signature block that spells the name three different ways — and because the
    domain is the single cheapest statement of where somebody works.
    """
    email: str = ""
    name: str = ""
    sent: int = 0
    received: int = 0
    last_sent: str = ""
    last_received: str = ""
    subjects: List[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        return self.email.partition("@")[2].lower()

    @property
    def company(self) -> str:
        """The employer implied by the address, when it implies one."""
        domain = self.domain
        if not domain or domain in CONSUMER_DOMAINS:
            return ""
        stem = domain.rsplit(".", 2)[0] if domain.count(".") > 1 else domain.split(".")[0]
        return stem.replace("-", " ").title()

    @property
    def named(self) -> str:
        """Their name, falling back to the one their address spells out.

        Most business addresses are the person's name with a dot in it, and a
        mail you sent says "Hi Mary," rather than introducing her — so the
        header is often the only place the name survives.
        """
        if self.name:
            return self.name
        local = self.email.partition("@")[0]
        parts = [p for p in re.split(r"[._\-]+", local) if p.isalpha() and len(p) > 1]
        if len(parts) < 2 or len(parts) > 3:
            return ""
        return " ".join(part.capitalize() for part in parts)

    @property
    def last(self) -> str:
        return max(self.last_sent, self.last_received)

    @property
    def two_way(self) -> bool:
        return self.sent > 0 and self.received > 0

    def days_since(self, today: Optional[dt.date] = None) -> Optional[int]:
        when = parse_date(self.last[:10])
        return None if not when else ((today or dt.date.today()) - when).days


def correspondents(messages: Sequence[Message], me: str = "") -> List[Correspondent]:
    """Everyone you have actually exchanged mail with.

    This is the list that stops a networking tool sending you back to people you
    wrote to last week — which is the failure that makes one useless on its
    second run.
    """
    found: Dict[str, Correspondent] = {}
    for message in messages:
        address = message.counterpart
        if not address or "@" not in address:
            continue
        if me and address == me.lower():
            continue
        if NOREPLY.match(address) or any(relay in address for relay in RELAY_SENDERS):
            continue
        if _ats_kind(message) or classify(message) == ALERT:
            continue
        entry = found.get(address)
        if entry is None:
            entry = Correspondent(email=address, name=extract_person(message))
            found[address] = entry
        if not entry.name:
            entry.name = extract_person(message)
        when = (message.date or "")[:10]
        if message.from_me:
            entry.sent += 1
            entry.last_sent = max(entry.last_sent, when)
        else:
            entry.received += 1
            entry.last_received = max(entry.last_received, when)
        subject = message.clean_subject
        if subject and subject not in entry.subjects:
            entry.subjects.append(subject)
    return sorted(found.values(), key=lambda c: c.last, reverse=True)


def within(messages: Sequence[Message], years: float,
           today: Optional[dt.date] = None) -> List[Message]:
    """Drop mail older than the window.

    A search moves. Roles from three years ago, and the people who were filling
    them, say less about today than they cost to read past.
    """
    if not years:
        return list(messages)
    cutoff = (today or dt.date.today()) - dt.timedelta(days=int(365.25 * years))
    kept = []
    for message in messages:
        when = message.when
        if when is None or when >= cutoff:
            kept.append(message)
    return kept


# --- does this role break your location rule --------------------------------

#: Ways a message states where the work happens. Deliberately narrow: this
#: fires only on an explicit statement, because the alternative — reading a
#: whole email as if it were a location field — turns any mention of a city
#: into a geographic restriction.
_PLACE_STATED = re.compile(
    r"(?:we(?:'re| are)\s+(?:in|based\s+in)|based\s+in|located\s+in|"
    r"office\s+in|position\s+in|role\s+is\s+(?:based\s+)?in|"
    r"on-?site\s+(?:in|at)|in-?person\s+(?:in|at))\s+([^.,;()\n]{2,40})", re.I)

_ONSITE = re.compile(r"\b(on-?site|in-?person|in the office|hybrid)\b", re.I)
_REMOTE = re.compile(r"\b(remote|work from home|wfh|distributed|remote-first)\b", re.I)


def location_warning(text: str, policy) -> str:
    """Why this role would not work for you, if the mail says plainly enough.

    Returns an empty string unless the message *states* a place you have ruled
    out. Silence is not evidence: most approaches never say where the work is,
    and guessing would either bury good roles or wave through bad ones. The
    honest position is to warn only where the mail is explicit.

    ``policy`` is the same :class:`LocationPolicy` that ``jobscout find``
    enforces, so the network and the job board answer to one rule.
    """
    from .filters import mentions_allowed_place

    if not text or policy is None:
        return ""
    places = [match.group(1).strip() for match in _PLACE_STATED.finditer(text)]
    if not places:
        return ""
    if any(mentions_allowed_place(place, policy) for place in places):
        return ""
    # A place you cannot reach is only a problem if you would have to be there.
    onsite = bool(_ONSITE.search(text))
    remote = bool(_REMOTE.search(text))
    if remote and not onsite:
        return ""
    if not onsite and not remote:
        return ""
    return "says %s, which is outside where you will work" % places[0][:40]


# --- what to do about it ---------------------------------------------------

#: A conversation that stopped this long ago can be reopened without it reading
#: as a nag. Shorter than this and you are chasing; much longer and the role
#: they had is certainly gone, though the relationship is not.
DORMANT_AFTER_DAYS = 30


@dataclass
class FollowUp:
    outreach: Outreach
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    killed: str = ""


def follow_ups(inbound: Sequence[Outreach],
               target_keys: Optional[Dict[str, str]] = None,
               applied_keys: Optional[Iterable[str]] = None,
               policy=None,
               killed=None,
               today: Optional[dt.date] = None) -> List[FollowUp]:
    """Rank inbound recruiters by who is worth a message this week.

    The ordering encodes what actually converts: someone who approached you and
    got no answer is owed nothing and costs nothing to revive; someone you
    talked to and liked is a relationship worth maintaining even though that
    particular role is gone; and anyone recruiting for a company already on
    your target list is worth more than either.
    """
    today = today or dt.date.today()
    target_keys = {normalize_company(k): v for k, v in (target_keys or {}).items()
                   if normalize_company(k)}
    applied = {normalize_company(k) for k in (applied_keys or []) if normalize_company(k)}
    ranked: List[FollowUp] = []

    for entry in inbound:
        item = FollowUp(outreach=entry)
        key = normalize_company(entry.company)

        if killed is not None:
            gone = killed.covers(person_ids=[entry.person], company=entry.company)
            if gone is not None:
                item.killed = gone.reason or "dismissed"
                item.score -= 500
                item.reasons.append("you ruled this out: %s" % item.killed)

        warning = location_warning(entry.said_about_place, policy)
        if warning:
            item.score -= 60
            item.reasons.append(warning)

        if key and key in target_keys:
            item.score += 40
            item.reasons.append("recruits for %s, which is on your target list" % entry.company)
        if key and key in applied:
            item.score += 25
            item.reasons.append("you applied to %s — they can find out where it went"
                                % entry.company)

        dormant = entry.dormant_days(today)
        if dormant is None:
            pass
        elif dormant < DORMANT_AFTER_DAYS:
            item.score -= 15
            item.reasons.append("spoke %d days ago — too soon to chase" % dormant)
        elif dormant <= 365:
            item.score += 20
            item.reasons.append("quiet for %d days — reopenable" % dormant)
        else:
            item.score += 8
            item.reasons.append("last spoke %.1f years ago" % (dormant / 365.25))

        if entry.outcome == "closed":
            item.score += 12
            item.reasons.append("that role died, but they still work your market")
        elif entry.replied:
            item.score += 15
            item.reasons.append("you already have a rapport — %d messages" % entry.messages)
        else:
            item.score += 18
            item.reasons.append("they approached you and you never answered")

        if entry.person:
            item.score += 5
        else:
            item.score -= 8
            item.reasons.append("no name recovered — open the thread before writing")

        ranked.append(item)

    ranked.sort(key=lambda f: (-f.score, f.outreach.last_contact), reverse=False)
    return ranked


# --- storage ---------------------------------------------------------------

def load_messages(path: Path) -> List[Message]:
    """Read an ingester's dump: one JSON file, or a folder of them."""
    path = Path(path).expanduser()
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    fields = set(Message.__dataclass_fields__)  # type: ignore[attr-defined]
    seen: Dict[str, Message] = {}
    for file in files:
        raw = json.loads(file.read_text(encoding="utf-8"))
        items = raw.get("messages", raw) if isinstance(raw, dict) else raw
        for item in items:
            message = Message(**{k: v for k, v in item.items() if k in fields})
            if message.id:
                seen[message.id] = message
    return sorted(seen.values(), key=lambda m: m.date or "")


def save_messages(path: Path, messages: Sequence[Message]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"captured": dt.date.today().isoformat(),
               "count": len(messages),
               "messages": [asdict(m) for m in messages]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path
