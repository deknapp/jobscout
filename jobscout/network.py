"""Your professional network, read from your own data export.

LinkedIn's search is not the tool for this and its API is closed, but the
platform will hand you your own connections as a CSV on request. That file has
everything the interesting question needs: who you know, where they work *now*,
what they do there, and the date you connected.

The date is the part people throw away, and it is the most useful column in the
file. Cross-referenced against your own employment history it says *how* you met
someone: a connection made while you worked somewhere is almost always from
there. If the company on their row today is a different one, that person has
moved on since — and the people who moved on are exactly the ones a stalled
search has not thought to ask, because you remember them at the old job.

Nothing here calls a model or the network. It is arithmetic over a file you
already own, which means it is free, instant, and works on a plane.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .corpus import normalize_company
from .models import parse_date

#: The columns LinkedIn ships. Spelling has drifted over the years, so each
#: field lists every header that has meant it.
_COLUMNS = {
    "first_name": ("first name",),
    "last_name": ("last name",),
    "url": ("url", "profile url"),
    "email": ("email address", "email"),
    "company": ("company", "current company"),
    "position": ("position", "current position", "title"),
    "connected_on": ("connected on", "connected"),
}

#: Titles that can open a door rather than just hold one. A director can refer
#: you into a req; a fellow individual contributor usually cannot, however much
#: they like you.
_LEVERAGE = re.compile(
    r"\b(chief|c[teiofr]o|founder|co-founder|owner|president|partner|"
    r"vp|vice president|head of|director|manager|lead|principal|staff|"
    r"distinguished|fellow|recruit\w*|talent|people ops|hiring)\b", re.I)

#: Recruiting and talent titles are leverage of a different kind — worth calling
#: out separately, because the message you send one is not the message you send
#: a former colleague.
_RECRUITING = re.compile(r"\b(recruit\w*|talent|sourcer|people ops|hr\b)\b", re.I)

#: A connection older than this has almost certainly changed something you do
#: not know about, whatever their row says.
DORMANT_YEARS = 3

#: Buckets, most actionable first. The bucket decides the message you write, so
#: they are named for the opening line rather than for the score.
INSIDE_TARGET = "inside-a-target"
MOVED_ON = "moved-on"
LEVERAGE_BUCKET = "hiring-power"
DOMAIN = "same-domain"
REST = "rest"

BUCKET_ORDER = (INSIDE_TARGET, MOVED_ON, LEVERAGE_BUCKET, DOMAIN, REST)

BUCKET_BLURB = {
    INSIDE_TARGET: "works at an employer you are already chasing",
    MOVED_ON: "met through a shared affiliation, and has since moved elsewhere",
    LEVERAGE_BUCKET: "senior enough in your field to refer or to hire",
    DOMAIN: "in your domain, no other signal",
    REST: "everyone else",
}


class NetworkError(RuntimeError):
    """Raised when an export cannot be read."""


# --- your own history ------------------------------------------------------

@dataclass
class Affiliation:
    """Somewhere *you* were, and when.

    Employers and schools both work: a connection made during a degree is as
    legible an origin story as one made during a job.
    """
    name: str
    start: Optional[str] = None      # ISO date or YYYY-MM
    end: Optional[str] = None        # None means "still there"
    kind: str = "employer"           # or "school"

    @property
    def key(self) -> str:
        return normalize_company(self.name)

    @property
    def start_date(self) -> Optional[dt.date]:
        return parse_date(self.start)

    @property
    def end_date(self) -> Optional[dt.date]:
        return parse_date(self.end)

    def covers(self, day: Optional[dt.date], slack_days: int = 90) -> bool:
        """Was this affiliation current on ``day``?

        The slack exists because people connect on the way in and on the way
        out — the recruiter you met a month before starting, the colleague who
        finally accepted your request after you both left.
        """
        if day is None:
            return False
        start = self.start_date
        end = self.end_date
        slack = dt.timedelta(days=slack_days)
        if start and day < start - slack:
            return False
        if end and day > end + slack:
            return False
        return bool(start or end)

    def span_years(self) -> float:
        start, end = self.start_date, self.end_date or dt.date.today()
        if not start:
            return 0.0
        return max((end - start).days, 0) / 365.25


def load_affiliations(path: Path) -> List[Affiliation]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NetworkError("%s is not valid JSON: %s" % (path, exc)) from exc
    items = raw.get("affiliations", raw if isinstance(raw, list) else [])
    fields = set(Affiliation.__dataclass_fields__)  # type: ignore[attr-defined]
    return [Affiliation(**{k: v for k, v in item.items() if k in fields})
            for item in items if item.get("name")]


def save_affiliations(path: Path, affiliations: Sequence[Affiliation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": dt.date.today().isoformat(),
               "affiliations": [asdict(a) for a in affiliations]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


# --- the export ------------------------------------------------------------

@dataclass
class Connection:
    first_name: str = ""
    last_name: str = ""
    url: str = ""
    email: str = ""
    company: str = ""
    position: str = ""
    connected_on: str = ""

    @property
    def name(self) -> str:
        return (" ".join(p for p in (self.first_name, self.last_name) if p)).strip()

    @property
    def company_key(self) -> str:
        return normalize_company(self.company)

    @property
    def connected_date(self) -> Optional[dt.date]:
        return parse_date(self.connected_on)

    @property
    def identity(self) -> str:
        """Stable across exports even if they change their name or company."""
        if self.url:
            return self.url.rstrip("/").lower()
        return ("%s|%s" % (self.name, self.email)).lower()


def _header_map(row: Sequence[str]) -> Dict[str, int]:
    lookup = {}
    for index, cell in enumerate(row):
        cell = (cell or "").strip().lstrip("﻿").lower()
        for field_name, aliases in _COLUMNS.items():
            if cell in aliases and field_name not in lookup:
                lookup[field_name] = index
    return lookup


def parse_connections_csv(text: str) -> List[Connection]:
    """Read ``Connections.csv``.

    The file does not start with its header: LinkedIn writes a short apology
    about the data being current as of the export date, then a blank line, then
    the real columns. Naive readers treat that preamble as the header and come
    back with one useless column, which is why this scans for the header rather
    than assuming a row number.
    """
    rows = list(csv.reader(io.StringIO(text)))
    header_at = -1
    lookup: Dict[str, int] = {}
    for index, row in enumerate(rows[:20]):
        candidate = _header_map(row)
        if "first_name" in candidate or ("company" in candidate and "position" in candidate):
            header_at, lookup = index, candidate
            break
    if header_at < 0:
        raise NetworkError(
            "no LinkedIn connection header found — expected a row naming "
            "First Name / Company / Position. Is this the right file?")

    def cell(row: Sequence[str], field_name: str) -> str:
        index = lookup.get(field_name, -1)
        if index < 0 or index >= len(row):
            return ""
        return (row[index] or "").strip()

    connections = []
    for row in rows[header_at + 1:]:
        if not any((c or "").strip() for c in row):
            continue
        connection = Connection(
            first_name=cell(row, "first_name"),
            last_name=cell(row, "last_name"),
            url=cell(row, "url"),
            email=cell(row, "email"),
            company=cell(row, "company"),
            position=cell(row, "position"),
            connected_on=cell(row, "connected_on"),
        )
        if connection.name or connection.url:
            connections.append(connection)
    return connections


def read_export(path: Path) -> List[Connection]:
    """Read connections from a zip, a folder, or the CSV itself.

    All three are what people actually have on disk ten minutes after asking
    for their data, so all three are accepted rather than documented against.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise NetworkError("%s does not exist" % path)

    if path.is_dir():
        for candidate in sorted(path.rglob("*.csv")):
            if candidate.name.lower().startswith("connections"):
                return parse_connections_csv(
                    candidate.read_text(encoding="utf-8", errors="replace"))
        raise NetworkError("no Connections.csv anywhere under %s" % path)

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist()
                     if Path(n).name.lower().startswith("connections")
                     and n.lower().endswith(".csv")]
            if not names:
                raise NetworkError(
                    "%s holds no Connections.csv. If you exported the full "
                    "archive it may still be building — the Connections-only "
                    "request is the fast one." % path.name)
            raw = archive.read(names[0])
        return parse_connections_csv(raw.decode("utf-8", errors="replace"))

    return parse_connections_csv(path.read_text(encoding="utf-8", errors="replace"))


# --- snapshots -------------------------------------------------------------

def snapshot_path(data_dir: Path, day: Optional[dt.date] = None) -> Path:
    day = day or dt.date.today()
    return data_dir / "network" / ("connections-%s.json" % day.isoformat())


def save_snapshot(data_dir: Path, connections: Sequence[Connection],
                  day: Optional[dt.date] = None) -> Path:
    path = snapshot_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"captured": (day or dt.date.today()).isoformat(),
               "count": len(connections),
               "connections": [asdict(c) for c in connections]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def list_snapshots(data_dir: Path) -> List[Path]:
    folder = data_dir / "network"
    if not folder.exists():
        return []
    return sorted(folder.glob("connections-*.json"))


def load_snapshot(path: Path) -> List[Connection]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    fields = set(Connection.__dataclass_fields__)  # type: ignore[attr-defined]
    return [Connection(**{k: v for k, v in item.items() if k in fields})
            for item in raw.get("connections", [])]


@dataclass
class Change:
    connection: Connection
    kind: str            # "moved", "promoted", "new"
    was_company: str = ""
    was_position: str = ""

    @property
    def summary(self) -> str:
        if self.kind == "new":
            return "new connection — %s at %s" % (
                self.connection.position or "role unknown",
                self.connection.company or "company unknown")
        if self.kind == "moved":
            return "%s → %s" % (self.was_company or "?", self.connection.company or "?")
        return "%s → %s (same employer)" % (
            self.was_position or "?", self.connection.position or "?")


def diff_snapshots(before: Sequence[Connection],
                   after: Sequence[Connection]) -> List[Change]:
    """What changed between two exports.

    This is the part that needs no inference at all, and it is why the first
    export is worth taking even though it tells you nothing on its own: it is
    the baseline every later one is read against.
    """
    old = {c.identity: c for c in before if c.identity}
    changes: List[Change] = []
    for connection in after:
        previous = old.get(connection.identity)
        if previous is None:
            changes.append(Change(connection, "new"))
            continue
        if previous.company_key != connection.company_key and connection.company:
            changes.append(Change(connection, "moved",
                                  was_company=previous.company,
                                  was_position=previous.position))
        elif (previous.position or "").strip().lower() != \
                (connection.position or "").strip().lower() and connection.position:
            changes.append(Change(connection, "promoted",
                                  was_company=previous.company,
                                  was_position=previous.position))
    return changes


# --- ranking ---------------------------------------------------------------

@dataclass
class Lead:
    connection: Connection
    score: int = 0
    bucket: str = REST
    anchor: str = ""                 # the affiliation you probably met through
    reasons: List[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.connection.name


def _domain_terms(profile: Optional[dict]) -> List[str]:
    """Words that mean "this person is in your world", taken from your profile.

    Deliberately drawn from the profile rather than a list in the source: the
    repo is public and the vocabulary of one search is nobody else's.
    """
    if not profile:
        return []
    text_fields: List[str] = []
    for key in ("target_titles", "adjacent_titles", "domains", "core_skills"):
        value = profile.get(key) or []
        if isinstance(value, list):
            text_fields.extend(str(v) for v in value)
    terms = set()
    for phrase in text_fields:
        for word in re.split(r"[^\w+#.]+", phrase.lower()):
            if len(word) > 3 and word not in _STOPWORDS:
                terms.add(word)
    return sorted(terms)


_STOPWORDS = {
    "senior", "staff", "principal", "lead", "engineer", "engineering", "developer",
    "manager", "director", "scientist", "with", "and", "the", "for", "from",
    "years", "level", "work", "team", "using", "role", "roles", "into", "over",
    "data", "software", "technical", "systems", "system", "design", "product",
}


def rank(connections: Sequence[Connection],
         affiliations: Sequence[Affiliation],
         target_keys: Optional[Dict[str, str]] = None,
         profile: Optional[dict] = None,
         today: Optional[dt.date] = None) -> List[Lead]:
    """Score every connection and sort the interesting ones to the top.

    ``target_keys`` maps a normalised company name to why you care about it
    ("applied", "tracked") — normally built from the jobscout employer registry,
    so the network view and the job view agree on what a target is.
    """
    today = today or dt.date.today()
    # Normalise defensively: a caller passing raw employer names would otherwise
    # match nothing at all and the whole view would look empty rather than broken.
    target_keys = {normalize_company(k): v for k, v in (target_keys or {}).items()
                   if normalize_company(k)}
    terms = set(_domain_terms(profile))
    leads: List[Lead] = []

    for connection in connections:
        lead = Lead(connection=connection)
        position = connection.position or ""
        company_key = connection.company_key

        # 1. Do they work somewhere you are actively trying to get into?
        target_reason = target_keys.get(company_key)
        if target_reason and company_key:
            weight = 55 if target_reason == "applied" else 40
            lead.score += weight
            lead.bucket = INSIDE_TARGET
            lead.reasons.append(
                "works at %s, which you have %s" %
                (connection.company,
                 "already applied to" if target_reason == "applied"
                 else "on your target list"))

        # 2. Where did you meet? And are they still there?
        anchor = _best_anchor(connection.connected_date, affiliations)
        if anchor:
            lead.anchor = anchor.name
            if company_key and company_key != anchor.key:
                lead.score += 25
                if lead.bucket == REST:
                    lead.bucket = MOVED_ON
                lead.reasons.append(
                    "connected while you were at %s; now at %s" %
                    (anchor.name, connection.company))
            elif company_key:
                lead.score += 5
                lead.reasons.append("still at %s, where you met" % anchor.name)

        # 3. Can they actually do anything for you?
        if _RECRUITING.search(position):
            lead.score += 20
            if lead.bucket in (REST, DOMAIN):
                lead.bucket = LEVERAGE_BUCKET
            lead.reasons.append("recruits for a living — ask them directly")
        elif _LEVERAGE.search(position):
            lead.score += 15
            if lead.bucket in (REST, DOMAIN):
                lead.bucket = LEVERAGE_BUCKET
            lead.reasons.append("senior enough to refer or to hire")

        # 4. Are they in your world at all?
        overlap = _term_overlap(position, terms)
        if overlap:
            lead.score += min(4 * len(overlap), 16)
            if lead.bucket == REST:
                lead.bucket = DOMAIN
            lead.reasons.append("your field: %s" % ", ".join(sorted(overlap)[:3]))

        # 5. Housekeeping signals.
        connected = connection.connected_date
        if connected:
            years = (today - connected).days / 365.25
            if years >= DORMANT_YEARS:
                lead.score += 5
                lead.reasons.append("connected %.0f years ago — long overdue" % years)
        if connection.email:
            lead.score += 5
            lead.reasons.append("you have their email")
        if not connection.company:
            lead.score -= 10
            lead.reasons.append("no current employer listed")

        leads.append(lead)

    leads.sort(key=lambda l: (-l.score, l.name.lower()))
    return leads


def _best_anchor(day: Optional[dt.date],
                 affiliations: Sequence[Affiliation]) -> Optional[Affiliation]:
    """Which of your own affiliations was current when you connected.

    Ties go to the shortest span: if a degree and a job overlap, the job is the
    more specific explanation for meeting a working professional.
    """
    candidates = [a for a in affiliations if a.covers(day)]
    if not candidates:
        return None
    employers = [a for a in candidates if a.kind == "employer"]
    pool = employers or candidates
    return min(pool, key=lambda a: a.span_years() or 99.0)


def _term_overlap(text: str, terms: Iterable[str]) -> List[str]:
    words = set(re.split(r"[^\w+#.]+", (text or "").lower()))
    return sorted(words & set(terms))


def by_bucket(leads: Sequence[Lead]) -> Dict[str, List[Lead]]:
    grouped: Dict[str, List[Lead]] = {b: [] for b in BUCKET_ORDER}
    for lead in leads:
        grouped.setdefault(lead.bucket, []).append(lead)
    return grouped


def company_coverage(connections: Sequence[Connection],
                     target_keys: Dict[str, str],
                     names: Optional[Dict[str, str]] = None
                     ) -> List[Tuple[str, List[Connection]]]:
    """For each employer you are chasing, who do you already know inside it.

    This is the view that changes what you do tomorrow: a target employer with
    one name against it is a warm introduction, and one with none is a cold
    application however good the posting looked.
    """
    names = names or {}
    inside: Dict[str, List[Connection]] = {key: [] for key in target_keys}
    for connection in connections:
        if connection.company_key in inside:
            inside[connection.company_key].append(connection)
    rows = [(names.get(key, key), people) for key, people in inside.items()]
    rows.sort(key=lambda row: (-len(row[1]), row[0].lower()))
    return rows
