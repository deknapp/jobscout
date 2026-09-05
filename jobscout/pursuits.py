"""What is actually live in your search, and what to do about it.

The other modules answer "who should I talk to". This one answers the harder
question a long search actually turns on: *what is still moving, who owes whom
a reply, and what is dead but hasn't been buried.*

That information exists only in ordinary mail between real people. There is no
sender to key on, no boilerplate to match — the decisive sentence in a live
process is something like "there's still no req posted, ping me in a week or
two", written by a hiring manager from her own address. Regular expressions
cannot read that, and pretending otherwise produced a company called "our
organization" on the first real message it saw.

So this module splits the work along the line each side is actually good at:

* **Python decides what the model may read.** Threads are grouped and filtered
  here — machine mail, alerts and consumer addresses never reach a prompt. That
  keeps the cost proportional and means the mailbox is not handed wholesale to
  anybody.
* **The model reads.** It extracts what the correspondence *says*: the stage,
  the requisition, who replied last, what the stated blocker is — each with a
  quote from the mail it came from.
* **Python decides what to do.** The recommendation is computed from those
  facts by rules you can read, in :func:`recommend`. A model asked to give
  advice gives different advice on Tuesday than on Monday; a rule does not, and
  a rule can be argued with.

The evidence quote is not decoration. Every claim this module makes can be
traced to a sentence somebody actually wrote, which is the only thing that
makes an automated read of your own job search worth trusting.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .corpus import normalize_company
from .inbox import (ALERT, APPLICATION, CONSUMER_DOMAINS, INTERVIEW, Message,
                    NOREPLY, REJECTION, RELAY_SENDERS, classify, extract_company,
                    extract_person, _ats_kind)
from .models import parse_date

# --- stages ----------------------------------------------------------------

#: Ordered from earliest to latest. A pursuit only ever moves forward, so when
#: two messages disagree the later stage wins.
STAGES = ("enquiry", "applied", "screening", "interviewing", "assignment",
          "offer", "closed")

#: Who is holding things up.
YOU, THEM, NOBODY = "you", "them", "nobody"


@dataclass
class Evidence:
    """One sentence somebody actually wrote, and when."""
    date: str = ""
    who: str = ""
    quote: str = ""


@dataclass
class Pursuit:
    """One process at one employer.

    Keyed on employer *and* requisition, because a lab running three searches
    is three pursuits: being turned down for one says nothing about the others,
    and merging them produces advice that is wrong in both directions.
    """
    company: str = ""
    role: str = ""
    requisition: str = ""
    people: List[str] = field(default_factory=list)
    stage: str = "enquiry"
    ball_with: str = THEM
    blocker: str = ""
    first_contact: str = ""
    last_activity: str = ""
    last_direction: str = ""          # "you" or "them"
    chased: int = 0                   # times you followed up with no reply
    evidence: List[Evidence] = field(default_factory=list)
    messages: int = 0

    @property
    def key(self) -> str:
        return "%s|%s" % (normalize_company(self.company), self.requisition.lower())

    def days_quiet(self, today: Optional[dt.date] = None) -> Optional[int]:
        when = parse_date((self.last_activity or "")[:10])
        return None if not when else ((today or dt.date.today()) - when).days


@dataclass
class Advice:
    pursuit: Pursuit
    urgency: str = "later"            # "now", "this week", "later", "none"
    action: str = ""
    why: str = ""

    @property
    def sort_key(self):
        order = {"now": 0, "this week": 1, "later": 2, "none": 3}
        return (order.get(self.urgency, 3), -(self.pursuit.days_quiet() or 0))


# --- what the model is allowed to see --------------------------------------

def is_human_mail(message: Message) -> bool:
    """Was this written by a person about your search.

    Everything excluded here is excluded before any prompt is built, so the
    filter is a cost control and a privacy boundary as much as a quality one.
    """
    address = message.counterpart
    if not address or "@" not in address:
        return False
    if NOREPLY.match(address):
        return False
    if any(relay in address for relay in RELAY_SENDERS):
        return True          # a recruiter through the relay is still a person
    domain = address.partition("@")[2].lower()
    if domain in CONSUMER_DOMAINS:
        # A recruiter writing from gmail is real; a note to your mother is not.
        return classify(message) not in (ALERT, "other")
    return classify(message) != ALERT


def company_of(message: Message) -> str:
    """Which employer a message belongs to, from the address before the prose.

    A person's own domain is worth more than anything the body says: mail from
    ``@lanl.gov`` is LANL even when the sentence reads "our organization".
    """
    address = message.counterpart
    domain = address.partition("@")[2].lower()
    if domain and domain not in CONSUMER_DOMAINS and not _ats_kind(message):
        stem = re.sub(r"^(mail|email|careers|jobs|talent|hr)\.", "", domain)
        stem = re.sub(r"\.(com|org|net|io|ai|co|gov|edu)(\.[a-z]{2})?$", "", stem)
        return stem.rsplit(".", 1)[-1].replace("-", " ")
    return extract_company(message)


def group(messages: Sequence[Message]) -> Dict[str, List[Message]]:
    """Bucket the mailbox by employer, keeping only what a person wrote."""
    buckets: Dict[str, List[Message]] = {}
    for message in messages:
        if not is_human_mail(message):
            continue
        company = company_of(message)
        if not company:
            continue
        buckets.setdefault(normalize_company(company), []).append(message)
    for thread in buckets.values():
        thread.sort(key=lambda m: m.date or "")
    return buckets


def digest(messages: Sequence[Message], max_chars: int = 700) -> str:
    """A compact, ordered transcript for the model to read."""
    lines = []
    for message in messages:
        body = re.sub(r"\s+", " ", (message.body or message.snippet or "")).strip()
        lines.append("[%s] %s (%s)\nSubject: %s\n%s"
                     % ((message.date or "")[:10],
                        "YOU wrote" if message.from_me else "THEY wrote",
                        message.counterpart or "unknown",
                        message.clean_subject, body[:max_chars]))
    return "\n\n".join(lines)


# --- the reading pass ------------------------------------------------------

SYSTEM = (
    "You read a job seeker's correspondence with one employer and report what "
    "it says. You are a careful reader, not an optimist and not an advisor. "
    "Never infer enthusiasm, interest or intent that is not written down. If "
    "the mail does not say something, the answer is the empty string. Every "
    "claim you make must be supported by a quote you copy verbatim from the "
    "mail."
)

PROMPT = """Below is the complete correspondence between a job seeker and people at one employer.

There may be MORE THAN ONE separate hiring process here — different roles, different requisition numbers, different teams. Report each separately. Being rejected for one requisition says nothing about another.

Today's date is %(today)s.

Return JSON: a list of objects, one per distinct hiring process, each with:
  "role":        the job title under discussion, or "" if never named
  "requisition": the req/job number if one appears, else ""
  "people":      list of names of the humans involved (not the seeker)
  "stage":       one of enquiry, applied, screening, interviewing, assignment, offer, closed
                 - "assignment" means they asked for work (code sample, take-home) — use it
                   only if the mail asks for one
                 - "closed" means an explicit rejection or withdrawal, not merely silence
  "ball_with":   "you" if the seeker owes a reply or a deliverable,
                 "them" if the seeker is waiting on the employer,
                 "nobody" if the process is closed
  "blocker":     the stated reason things are not moving, in the employer's own terms,
                 e.g. "no requisition posted yet". "" if none is stated.
  "evidence":    list of up to 3 objects {"date","who","quote"} — quote copied
                 EXACTLY from the mail, each supporting one of your fields above

Correspondence with %(company)s:

%(digest)s
"""


def read(messages: Sequence[Message], company: str, llm, today: Optional[dt.date] = None
         ) -> List[Pursuit]:
    """Ask the model what this correspondence says. One call per employer."""
    today = today or dt.date.today()
    if not messages:
        return []
    payload = llm.ask_json(PROMPT % {"today": today.isoformat(),
                                     "company": company,
                                     "digest": digest(messages)},
                           system=SYSTEM)
    if isinstance(payload, dict):
        payload = [payload]
    found: List[Pursuit] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "enquiry").strip().lower()
        pursuit = Pursuit(
            company=company,
            role=str(item.get("role") or "").strip(),
            requisition=str(item.get("requisition") or "").strip(),
            people=[str(p) for p in (item.get("people") or []) if p],
            stage=stage if stage in STAGES else "enquiry",
            ball_with=str(item.get("ball_with") or THEM).strip().lower(),
            blocker=str(item.get("blocker") or "").strip(),
            evidence=[Evidence(date=str(e.get("date") or ""),
                               who=str(e.get("who") or ""),
                               quote=str(e.get("quote") or ""))
                      for e in (item.get("evidence") or []) if isinstance(e, dict)],
            messages=len(messages),
        )
        if pursuit.ball_with not in (YOU, THEM, NOBODY):
            pursuit.ball_with = THEM
        _fill_dates(pursuit, messages)
        found.append(pursuit)
    return found


def _fill_dates(pursuit: Pursuit, messages: Sequence[Message]) -> None:
    """Dates come from the headers, never from the model.

    A model asked for a date will produce a plausible one. The mailbox already
    knows, so it is never asked.
    """
    dated = [m for m in messages if m.date]
    if not dated:
        return
    pursuit.first_contact = dated[0].date[:10]
    last = dated[-1]
    pursuit.last_activity = last.date[:10]
    pursuit.last_direction = YOU if last.from_me else THEM
    # A chase is a message you sent that nobody answered.
    pursuit.chased = 0
    for index, message in enumerate(dated):
        if message.from_me and all(m.from_me for m in dated[index + 1:]):
            pursuit.chased += 1


# --- what to do about it ---------------------------------------------------

#: Below this many days, following up reads as impatience rather than interest.
TOO_SOON = 7

#: Beyond this, a silence is information in itself.
GONE_QUIET = 21


def recommend(pursuit: Pursuit, today: Optional[dt.date] = None) -> Advice:
    """Turn what the mail says into what to do, by rules rather than by vibe.

    Every branch here is arguable, which is the point: you can disagree with a
    rule and change it. The alternative — asking a model for advice — gives an
    answer that sounds equally confident whatever the facts, and a different
    one tomorrow.
    """
    today = today or dt.date.today()
    quiet = pursuit.days_quiet(today)
    who = pursuit.people[0] if pursuit.people else "them"
    advice = Advice(pursuit=pursuit)

    if pursuit.stage == "closed":
        advice.urgency = "none"
        advice.action = "Closed. Keep %s as a contact — they still work this market." % who
        advice.why = pursuit.blocker or "They said no."
        return advice

    if pursuit.stage == "assignment" and pursuit.ball_with == YOU:
        advice.urgency = "now"
        advice.action = "Send what %s asked for. Nothing else in this pursuit matters until it lands." % who
        advice.why = pursuit.blocker or "They are waiting on a deliverable from you."
        return advice

    if pursuit.ball_with == YOU:
        advice.urgency = "now"
        advice.action = "Reply to %s — they are waiting on you." % who
        advice.why = ("They wrote %s days ago and you have not answered." % quiet
                      if quiet is not None else "You owe the last reply.")
        return advice

    # Waiting on them.
    if quiet is None:
        advice.urgency = "later"
        advice.action = "No dates in this thread — open it before acting."
        return advice

    if pursuit.blocker:
        # A stated structural blocker is not a silence problem, and chasing it
        # harder does not move it. This is the Lily Kim case: the honest read is
        # that there is nothing to chase, and the useful move is sideways.
        advice.urgency = "later" if quiet < GONE_QUIET else "this week"
        advice.action = ("Do not chase the blocker — ask someone else inside %s "
                         "whether it is real." % pursuit.company)
        advice.why = "They told you why it is stalled: %s" % pursuit.blocker
        return advice

    if quiet < TOO_SOON:
        advice.urgency = "none"
        advice.action = "Wait. It has only been %d days." % quiet
        advice.why = "Following up this soon reads as impatience."
        return advice

    if quiet <= GONE_QUIET:
        advice.urgency = "this week"
        advice.action = "Follow up with %s." % who
        advice.why = "Quiet %d days, and you have chased %d time(s)." % (quiet, pursuit.chased)
        return advice

    if pursuit.chased >= 2:
        advice.urgency = "none"
        advice.action = "Treat as dead. Two unanswered follow-ups is an answer."
        advice.why = "Quiet %d days after %d follow-ups." % (quiet, pursuit.chased)
        return advice

    advice.urgency = "this week"
    advice.action = "One last note to %s, then close it out." % who
    advice.why = "Quiet %d days." % quiet
    return advice


def review(messages: Sequence[Message], llm, today: Optional[dt.date] = None,
           only: Optional[Iterable[str]] = None) -> List[Advice]:
    """Read the whole mailbox and say what to do about each live process."""
    today = today or dt.date.today()
    wanted = {normalize_company(name) for name in (only or []) if name}
    advice: List[Advice] = []
    for key, thread in group(messages).items():
        if wanted and key not in wanted:
            continue
        company = company_of(thread[0]) or key
        for pursuit in read(thread, company, llm, today=today):
            advice.append(recommend(pursuit, today=today))
    advice.sort(key=lambda a: a.sort_key)
    return advice
