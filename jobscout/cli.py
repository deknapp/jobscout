"""The command line.

    jobscout init --applications "~/path/to/your applications" --states XX
    jobscout status
    jobscout profile [--refresh]
    jobscout companies [--add NAME] [--ignore NAME] [--forget NAME]
    jobscout find [--expand] [--max 10]
    jobscout serve [--port 8765]
    jobscout history [--status recommended]
    jobscout mark <id> --applied | --dismissed [--note "..."]
    jobscout network import <export.zip|Connections.csv|folder>
    jobscout network leads [--bucket moved-on] [--max 40]
    jobscout network coverage
    jobscout network changes
    jobscout network me --add "Employer" --from 2020-08 --to 2024-05
    jobscout inbox applications | recruiters | contacts
    jobscout pursuits [--company NAME] [--all]
    jobscout dismiss "<who or where>" --reason "..." [--employer]
    jobscout ingest --mbox ~/export.mbox --me you@example.com
    jobscout ingest --imap --user you@gmail.com   (app password in the env)
    jobscout brief
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__, report
from .companies import Company, IGNORED, NEW, Registry
from .config import (ConfigError, DEFAULT_DATA_DIR, ENV_FILE, LocationPolicy, Settings,
                     load_settings, redact, save_location_policy)
from .corpus import load_corpus, summarize
from .history import APPLIED, DISMISSED, History, RECOMMENDED
from .llm import LLMError
from .models import Posting
from .network import BUCKET_ORDER as NETWORK_BUCKETS
from .pipeline import find as run_find, load_or_build_profile

ENV_TEMPLATE = """# jobscout configuration. This file is git-ignored — keep it that way.

# The folder holding the applications you have already written.
# One subfolder per company works best. It must NOT be inside the jobscout repo.
JOBSCOUT_APPLICATIONS_DIR={applications}

# Where jobscout keeps your profile, employer registry, history and reports.
JOBSCOUT_DATA_DIR={data}

# "cli" bills your logged-in Claude Code account and gets hosted web search.
# "anthropic" uses ANTHROPIC_API_KEY instead. "mock" runs offline for free.
JOBSCOUT_BACKEND=cli
JOBSCOUT_MODEL_CHEAP=claude-haiku-4-5
JOBSCOUT_MODEL_STRONG=claude-opus-5

# Only needed for `jobscout ingest --imap`. A Gmail APP password, not your
# account password — create one at https://myaccount.google.com/apppasswords.
# Leave it unset and you will be prompted instead, which keeps it out of files.
# JOBSCOUT_IMAP_PASSWORD=

# Hard filters.
JOBSCOUT_ALLOWED_STATES={states}
JOBSCOUT_ALLOWED_CITIES={cities}
JOBSCOUT_ALLOW_REMOTE={remote}
JOBSCOUT_ALLOW_HYBRID=false
JOBSCOUT_MAX_AGE_DAYS=30

# Per-run budget. LEAVE THESE COMMENTED OUT unless you mean to override the
# built-in defaults — a value written here wins forever, and pinning one to
# whatever the default happened to be on the day you ran `init` silently
# freezes the tool at that version's behaviour.
# JOBSCOUT_COMPANY_TARGET=120
# JOBSCOUT_MAX_RESOLVE_PER_RUN=20
# JOBSCOUT_MAX_SCANS_PER_RUN=8
# JOBSCOUT_MAX_VERIFY_PER_RUN=20
# JOBSCOUT_MAX_RESULTS=10
# JOBSCOUT_MAX_WORKERS=6

# Ranking: how well you match, your realistic odds, and how fresh the posting is.
JOBSCOUT_WEIGHT_FIT=0.45
JOBSCOUT_WEIGHT_LIKELIHOOD=0.30
JOBSCOUT_WEIGHT_RECENCY=0.25
JOBSCOUT_RECENCY_HALFLIFE_DAYS=14

# The local web app.
JOBSCOUT_PORT=8765

# Employers you never want to see, comma separated.
JOBSCOUT_EXCLUDE_COMPANIES=
"""


def _fail(message: str) -> int:
    sys.stderr.write("error: %s\n" % message)
    return 1


# --- init ------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    applications = Path(os.path.expanduser(args.applications)).resolve() if args.applications else None
    data_dir = Path(os.path.expanduser(args.data_dir)).resolve()

    if applications is not None and not applications.is_dir():
        return _fail("applications folder does not exist: %s" % applications)

    if ENV_FILE.exists() and not args.force:
        return _fail("%s already exists (use --force to overwrite)" % redact(ENV_FILE))

    states = ",".join(s.strip().upper() for s in (args.states or "").split(",") if s.strip())
    cities = ",".join(c.strip() for c in (args.cities or "").split(",") if c.strip())
    ENV_FILE.write_text(ENV_TEMPLATE.format(
        applications=('"%s"' % applications) if applications else "",
        data=data_dir,
        states=states,
        cities=cities,
        remote="true" if not args.no_remote else "false",
    ), encoding="utf-8")

    data_dir.mkdir(parents=True, exist_ok=True)
    policy = LocationPolicy(
        allowed_states=[s for s in states.split(",") if s],
        allowed_cities=[c for c in cities.split(",") if c],
        allow_remote=not args.no_remote,
        allow_hybrid=False,
        description=args.describe or "",
    )
    policy_path = save_location_policy(data_dir, policy.normalized())

    print("wrote %s" % redact(ENV_FILE))
    print("wrote %s" % redact(policy_path))
    print("data dir: %s" % redact(data_dir))
    if applications:
        print("applications: %s" % redact(applications))
    print()
    print("Next: `jobscout status` to check what it can read, then `jobscout find`.")
    return 0


# --- status ----------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(require_applications=False)
    print("jobscout %s" % __version__)
    print("  backend       %s (cheap=%s, strong=%s)"
          % (settings.backend, settings.model_cheap, settings.model_strong))
    print("  applications  %s" % redact(settings.applications_dir))
    print("  data dir      %s" % redact(settings.data_dir))
    print("  location      %s" % settings.location.summary())
    print("      states    %s" % (", ".join(settings.location.allowed_states) or "(none)"))
    print("      cities    %s" % (", ".join(settings.location.allowed_cities) or "(none)"))
    print("      remote    %s / hybrid %s"
          % (settings.location.allow_remote, settings.location.allow_hybrid))
    print("  freshness     postings at most %d days old" % settings.max_age_days)
    print()

    if settings.applications_dir.is_dir():
        corpus = load_corpus(settings.applications_dir)
        print(summarize(corpus))
    else:
        print("applications folder not readable — run `jobscout init`")
    print()

    registry = Registry(settings.companies_path)
    print("employers: %s" % registry.summary())
    history = History(settings.history_path)
    print("history:   %d entry(ies) — %d recommended, %d applied, %d dismissed"
          % (len(history.entries), len(history.by_status(RECOMMENDED)),
             len(history.by_status(APPLIED)), len(history.by_status(DISMISSED))))
    print("profile:   %s" % ("built" if settings.profile_path.exists() else "not built yet"))
    return 0


# --- profile ---------------------------------------------------------------

def cmd_profile(args: argparse.Namespace) -> int:
    from .llm import LLM

    settings = load_settings()
    settings.ensure_data_dir()
    corpus = load_corpus(settings.applications_dir)
    llm = LLM.from_settings(settings)
    profile = load_or_build_profile(settings, llm, corpus, refresh=args.refresh)
    print(json.dumps(profile, indent=2, ensure_ascii=False))
    if args.refresh:
        sys.stderr.write("\n%s\n" % llm.usage.summary())
    return 0


# --- companies -------------------------------------------------------------

def cmd_companies(args: argparse.Namespace) -> int:
    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    registry = Registry(settings.companies_path)

    changed = False
    for name in args.add or []:
        registry.add(Company(name=name, why="added by hand"))
        changed = True
        print("added %s" % name)
    for name in args.ignore or []:
        company = registry.get(name) or registry.add(Company(name=name))
        company.status = IGNORED
        changed = True
        print("ignoring %s" % company.name)
    for name in args.reresolve or []:
        company = registry.get(name)
        if company is None:
            print("no such employer: %s" % name)
            continue
        company.careers_url = ""
        company.ats = ""
        company.status = NEW
        company.last_scanned = ""
        changed = True
        print("will look up %s's board again on the next run" % company.name)
    for name in args.forget or []:
        company = registry.get(name)
        if company is not None:
            del registry.companies[company.key]
            changed = True
            print("forgot %s" % company.name)
    if changed:
        registry.save()
        return 0

    companies = registry.sorted()
    if not companies:
        print("no employers yet — `jobscout find` will propose some")
        return 0
    print("%-34s %-11s %-38s %s" % ("EMPLOYER", "STATUS", "BOARD", "LAST SCAN"))
    for company in companies:
        print("%-34s %-11s %-38s %s"
              % (company.name[:34], company.status,
                 (company.careers_url or "—")[:38], company.last_scanned or "never"))
    print()
    print(registry.summary())
    return 0


# --- find ------------------------------------------------------------------

def cmd_find(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.max:
        settings.max_results = args.max
    if args.max_age:
        settings.max_age_days = args.max_age

    result = run_find(settings, refresh_profile=args.refresh_profile, expand=args.expand)
    text = report.render(
        result.recommended, result.dropped, result.stats,
        usage=result.usage_summary, errors=result.errors,
        deferred=result.deferred, location_summary=settings.location.summary(),
        weights_summary=settings.weights().describe(),
        requirements_summary=settings.requirements.summary())
    print(text)
    if not args.no_save:
        path = report.write(text, settings.reports_dir)
        sys.stderr.write("\nreport saved to %s\n" % redact(path))
    sys.stderr.write("%s\n" % result.usage_summary)
    return 0


# --- filters ---------------------------------------------------------------

def cmd_filters(args: argparse.Namespace) -> int:
    from .config import save_requirements
    from .requirements import EXCLUDE, INCLUDE

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    requirements = settings.requirements

    if args.clear:
        from .requirements import Requirements

        requirements = Requirements()

    changed = args.clear
    def money(value):
        if value is None:
            return None
        text = str(value).strip().lower().replace("$", "").replace(",", "")
        if text in ("none", "any", "-", ""):
            return None
        return int(float(text.replace("k", "000")))

    for flag, attr, convert in (
            ("salary_min", "salary_min", money),
            ("salary_max", "salary_max", money),
            ("unknown_salary", "unknown_salary", str),
            ("unknown_employment", "unknown_employment", str),
            ("unknown_clearance", "unknown_clearance", str),
            ("unknown_location", "unknown_location", str),
            ("unknown_date", "unknown_date", str)):
        value = getattr(args, flag)
        if value is not None:
            setattr(requirements, attr, convert(value))
            changed = True
    if args.employment is not None:
        requirements.employment_types = [t.strip() for t in args.employment.split(",") if t.strip()]
        changed = True
    if args.exclude_clearance is not None:
        requirements.exclude_clearance_required = args.exclude_clearance
        changed = True
    if args.exclude_title is not None:
        requirements.exclude_title_words = [w.strip() for w in args.exclude_title.split(",") if w.strip()]
        changed = True
    if args.require_title is not None:
        requirements.require_title_words = [w.strip() for w in args.require_title.split(",") if w.strip()]
        changed = True

    requirements = requirements.normalized()
    if changed:
        path = save_requirements(settings.data_dir, requirements)
        print("saved %s" % redact(path))
        print()

    print("Location (hard):   %s" % settings.location.summary())
    print("Freshness:         at most %d days old" % settings.max_age_days)
    print()
    print("Salary:            %s to %s" % (
        "$%s" % f"{requirements.salary_min:,}" if requirements.salary_min else "any",
        "$%s" % f"{requirements.salary_max:,}" if requirements.salary_max else "any"))
    print("Employment type:   %s" % (", ".join(requirements.employment_types) or "any"))
    print("Clearance roles:   %s" % ("excluded" if requirements.exclude_clearance_required
                                     else "allowed"))
    print("Title excludes:    %s" % (", ".join(requirements.exclude_title_words) or "—"))
    print("Title must match:  %s" % (", ".join(requirements.require_title_words) or "—"))
    print()
    print("When a posting DOESN'T SAY — a filter that silently drops these is")
    print("the reason good jobs vanish, so each one is your call:")
    print("  no salary stated        %s" % requirements.unknown_salary)
    print("  no employment type      %s" % requirements.unknown_employment)
    print("  no clearance statement  %s" % requirements.unknown_clearance)
    print("  no location stated      %s" % requirements.unknown_location)
    print("  no posting date         %s" % requirements.unknown_date)
    return 0


# --- serve -----------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    """The web app configures itself, so nothing needs to be set up first."""
    from .web import serve

    settings = load_settings(require_applications=False)
    return serve(settings, port=args.port, open_browser=not args.no_browser)


# --- history ---------------------------------------------------------------

def cmd_history(args: argparse.Namespace) -> int:
    settings = load_settings(require_applications=False)
    history = History(settings.history_path)
    if args.retry:
        removed = history.forget_transient()
        print("cleared %d soft rejection(s) — they will be reconsidered next run"
              % removed)
        return 0
    entries = history.entries
    if args.status:
        entries = [e for e in entries if e.status == args.status]
    if not entries:
        print("nothing recorded yet")
        return 0
    entries = sorted(entries, key=lambda e: e.last_seen or e.first_seen, reverse=True)
    for entry in entries[:args.limit]:
        print("%s  %-11s %-26s %-42s %s"
              % (entry.id, entry.status, entry.company[:26], entry.title[:42],
                 entry.last_seen or entry.first_seen))
        if args.verbose and entry.reason:
            print("            %s" % entry.reason)
    print()
    print("%d of %d entry(ies)" % (min(len(entries), args.limit), len(entries)))
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    settings = load_settings(require_applications=False)
    history = History(settings.history_path)
    status = APPLIED if args.applied else DISMISSED
    entry = history.mark(args.id, status, note=args.note or "")
    if entry is None:
        return _fail("no history entry matching id %r (try `jobscout history`)" % args.id)
    print("%s → %s (%s — %s)" % (entry.id, status, entry.company, entry.title))
    return 0


# --- parser ----------------------------------------------------------------

# --- network ---------------------------------------------------------------

def _killed(settings):
    from .dismissed import Dismissals

    return Dismissals(settings.dismissed_path)


def _contact_history(settings):
    """Everyone you have already spoken to, from LinkedIn and from your mail."""
    from . import inbox as box, network as net

    talked = net.load_conversations(settings.data_dir)
    if settings.inbox_dir.exists():
        messages = box.load_messages(settings.inbox_dir)
        if messages:
            talked.update(net.conversations_from_mail(box.correspondents(messages)))
    return talked


def _network_context(settings):
    """Everything the ranking needs, gathered from what jobscout already knows."""
    from .network import load_affiliations

    affiliations = load_affiliations(settings.affiliations_path)
    registry = Registry(settings.companies_path)
    targets = {}
    names = {}
    for company in registry.sorted():
        if company.status == IGNORED or not company.key:
            continue
        targets[company.key] = "tracked"
        names[company.key] = company.name
    profile = {}
    if settings.profile_path.exists():
        try:
            profile = json.loads(settings.profile_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            profile = {}
    # An employer you have actually written to outranks one merely on the list.
    from .corpus import normalize_company
    for name in profile.get("applied_companies", []) or []:
        key = normalize_company(name)
        if key:
            targets[key] = "applied"
            names.setdefault(key, name)
    return affiliations, targets, names, profile


def _latest_connections(settings):
    from .network import list_snapshots, load_snapshot

    snapshots = list_snapshots(settings.data_dir)
    if not snapshots:
        return [], None
    return load_snapshot(snapshots[-1]), snapshots[-1]


def cmd_network(args: argparse.Namespace) -> int:
    from . import network as net

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    action = args.action

    if action == "import":
        connections = net.read_export(Path(args.path))
        path = net.save_snapshot(settings.data_dir, connections)
        talked = net.read_conversations(Path(args.path))
        if talked:
            net.save_conversations(settings.data_dir, talked)
        dated = sum(1 for c in connections if c.connected_date)
        employers = len({c.company_key for c in connections if c.company_key})
        print("read %d connection(s) from %s" % (len(connections), Path(args.path).name))
        print("  %d have a connection date, %d distinct employers" % (dated, employers))
        print("  saved to %s" % redact(path))
        if talked:
            two_way = sum(1 for c in talked.values() if c.two_way)
            print("  read %d conversation(s) you have already had — %d of them two-way"
                  % (len(talked), two_way))
        older = net.list_snapshots(settings.data_dir)
        if len(older) > 1:
            print("  run `jobscout network changes` to diff against %s" % older[-2].name)
        else:
            print("  this is your baseline — export again in a month and "
                  "`jobscout network changes` will name everyone who moved")
        if not settings.affiliations_path.exists():
            print("\nnext: tell it where you have worked, so it can work out how you\n"
                  "met people and who has moved on since:\n"
                  '  jobscout network me --add "Employer" --from 2020-08 --to 2024-05')
        return 0

    if action == "me":
        affiliations = net.load_affiliations(settings.affiliations_path)
        if args.from_export:
            found = net.read_positions(Path(args.from_export))
            if not found:
                return _fail("no Positions.csv or Education.csv under %s"
                             % redact(Path(args.from_export)))
            net.save_affiliations(settings.affiliations_path, found)
            print("read %d affiliation(s) from your export" % len(found))
            for a in found:
                print("  %-40s %-9s %s to %s"
                      % (a.name[:40], a.kind, a.start or "?", a.end or "present"))
            return 0
        if args.add:
            affiliations = [a for a in affiliations if a.name.lower() != args.add.lower()]
            affiliations.append(net.Affiliation(
                name=args.add, start=args.since, end=args.until, kind=args.kind))
            net.save_affiliations(settings.affiliations_path, affiliations)
            print("recorded %s (%s, %s to %s)"
                  % (args.add, args.kind, args.since or "?", args.until or "present"))
            return 0
        if args.remove:
            kept = [a for a in affiliations if a.name.lower() != args.remove.lower()]
            if len(kept) == len(affiliations):
                print("no affiliation named %s" % args.remove)
                return 1
            net.save_affiliations(settings.affiliations_path, kept)
            print("removed %s" % args.remove)
            return 0
        if not affiliations:
            print("no history recorded yet. Add one:")
            print('  jobscout network me --add "Employer" --from 2020-08 --to 2024-05')
            return 0
        print("%-38s %-10s %-10s %s" % ("AFFILIATION", "KIND", "FROM", "TO"))
        for a in sorted(affiliations, key=lambda x: x.start or ""):
            print("%-38s %-10s %-10s %s"
                  % (a.name[:38], a.kind, a.start or "?", a.end or "present"))
        return 0

    connections, snapshot = _latest_connections(settings)
    if not connections:
        return _fail("no connections imported yet — "
                     "`jobscout network import <your export>` first")
    affiliations, targets, names, profile = _network_context(settings)

    if action == "coverage":
        rows = net.company_coverage(connections, targets, names)
        have = [r for r in rows if r[1]]
        print("Employers you are chasing, and who you already know inside them")
        print("(%d of %d have someone)\n" % (len(have), len(rows)))
        for name, people in rows:
            if not people:
                continue
            print("%s — %d" % (name, len(people)))
            for person in people[:6]:
                print("    %-28s %s" % (person.name[:28], (person.position or "")[:44]))
            if len(people) > 6:
                print("    ... and %d more" % (len(people) - 6))
            print()
        cold = [name for name, people in rows if not people]
        if cold:
            print("No path in (cold application territory): %s"
                  % ", ".join(sorted(cold)[:20]))
        return 0

    if action == "changes":
        snapshots = net.list_snapshots(settings.data_dir)
        if len(snapshots) < 2:
            print("only one export so far (%s) — nothing to diff against yet."
                  % snapshots[0].name)
            print("Export again in a few weeks; job changes fall out of the diff.")
            return 0
        before = net.load_snapshot(snapshots[-2])
        changes = net.diff_snapshots(before, connections)
        moved = [c for c in changes if c.kind == "moved"]
        promoted = [c for c in changes if c.kind == "promoted"]
        fresh = [c for c in changes if c.kind == "new"]
        print("%s → %s\n" % (snapshots[-2].name, snapshots[-1].name))
        for label, group in (("Changed employer", moved), ("New title", promoted),
                             ("New connections", fresh)):
            if not group:
                continue
            print("%s (%d)" % (label, len(group)))
            for change in group[:args.max]:
                print("  %-26s %s" % (change.connection.name[:26], change.summary))
            print()
        if moved:
            print("Anyone in the first list is worth a note this week — a job "
                  "change is the one moment\ncold-sounding outreach reads as "
                  "congratulations.")
        return 0

    # default: leads
    leads = net.rank(connections, affiliations, targets, profile,
                     conversations=_contact_history(settings),
                     killed=_killed(settings))
    if args.bucket:
        leads = [l for l in leads if l.bucket == args.bucket]
    if args.company:
        from .corpus import normalize_company
        key = normalize_company(args.company)
        leads = [l for l in leads if l.connection.company_key == key]
    buried = [l for l in leads if l.killed]
    if not args.all:
        leads = [l for l in leads if l.bucket != net.REST and not l.killed]
    shown = leads[:args.max]
    if not shown:
        print("nothing matched")
        return 0

    if not affiliations:
        sys.stderr.write(
            "note: no work history recorded, so nobody can be identified as "
            "having moved on.\n      jobscout network me --add \"Employer\" "
            "--from YYYY-MM --to YYYY-MM\n\n")

    grouped = net.by_bucket(shown)
    for bucket in net.BUCKET_ORDER:
        people = grouped.get(bucket) or []
        if not people:
            continue
        print("== %s — %s (%d)" % (bucket, net.BUCKET_BLURB[bucket], len(people)))
        for lead in people:
            connection = lead.connection
            print("  %3d  %-26s %s"
                  % (lead.score, connection.name[:26],
                     ("%s @ %s" % (connection.position or "?",
                                   connection.company or "?"))[:60]))
            for reason in lead.reasons[:3]:
                print("       - %s" % reason)
            if connection.url:
                print("       %s" % connection.url)
        print()
    print("%d shown of %d connection(s)%s."
          % (len(shown), len(connections),
             "; %d you ruled out (--all to see)" % len(buried) if buried else ""))
    return 0


# --- inbox -----------------------------------------------------------------

def cmd_inbox(args: argparse.Namespace) -> int:
    from . import inbox as box

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    source = Path(args.path).expanduser() if args.path else settings.inbox_dir
    if not source.exists():
        return _fail("no mail ingested yet — nothing at %s" % redact(source))
    messages = box.load_messages(source)
    if not messages:
        return _fail("no messages found in %s" % redact(source))

    if args.action == "applications":
        found = box.applications(messages)
        if not found:
            print("no applications recognised in %d message(s)" % len(messages))
            return 0
        print("%-30s %-34s %-12s %s" % ("EMPLOYER", "ROLE", "APPLIED", "OUTCOME"))
        for entry in found:
            print("%-30s %-34s %-12s %s"
                  % (entry.company[:30], (entry.role or "—")[:34],
                     entry.applied or "—", entry.status))
        silent = [e for e in found if e.status == "no answer"]
        print("\n%d application(s); %d never answered." % (len(found), len(silent)))
        return 0

    if args.action == "contacts":
        found = box.contacts(messages)
        if not found:
            print("no named humans recovered")
            return 0
        print("%-26s %-28s %-28s %s" % ("NAME", "COMPANY", "HOW YOU KNOW THEM", "LAST SEEN"))
        for person in found:
            print("%-26s %-28s %-28s %s"
                  % (person.name[:26], (person.company or "—")[:28],
                     person.context[:28], person.seen))
        return 0

    # default: recruiters, ranked
    affiliations, targets, names, profile = _network_context(settings)
    applied = [a.company for a in box.applications(messages)]
    applied += profile.get("applied_companies", []) or []
    ranked = box.follow_ups(box.outreach(messages), target_keys=targets,
                            applied_keys=applied, policy=settings.location,
                            killed=_killed(settings))
    if args.max:
        ranked = ranked[:args.max]
    if not ranked:
        print("no inbound recruiters found")
        return 0
    for item in ranked:
        entry = item.outreach
        who = entry.person or "(name unknown)"
        print("%4d  %-24s %s" % (item.score, who[:24],
                                 (entry.role or entry.company or "—")[:52]))
        for reason in item.reasons[:3]:
            print("        - %s" % reason)
        print("        last contact %s, %d message(s)%s"
              % (entry.last_contact, entry.messages,
                 ", you replied" if entry.replied else ", you never replied"))
        print()
    print("%d inbound approach(es)." % len(ranked))
    return 0


# --- pursuits --------------------------------------------------------------

def cmd_pursuits(args: argparse.Namespace) -> int:
    from . import inbox as box, pursuits as live
    from .llm import LLM

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    source = Path(args.path).expanduser() if args.path else settings.inbox_dir
    if not source.exists():
        return _fail("no mail ingested yet — nothing at %s" % redact(source))
    messages = box.load_messages(source)
    if args.since_years:
        messages = box.within(messages, args.since_years)
    if not messages:
        return _fail("no messages in the window")

    llm = LLM.from_settings(settings)
    only = [args.company] if args.company else None
    sys.stderr.write("reading %d message(s) across %d employer(s)…\n"
                     % (len(messages), len(live.group(messages))))
    from .aliases import Aliases

    advice = live.review(messages, llm, only=only, killed=_killed(settings),
                         aliases=Aliases(settings.aliases_path))
    if not args.company:
        live.save(settings.pursuits_path, advice)
    if not advice:
        print("nothing live found")
        return 0

    shown = 0
    for item in advice:
        if not args.all and item.urgency == "none":
            continue
        shown += 1
        pursuit = item.pursuit
        print("[%s] %s" % (item.urgency.upper(), pursuit.label))
        print("    stage %s, ball with %s, quiet %s day(s)"
              % (pursuit.stage, pursuit.ball_with,
                 pursuit.days_quiet() if pursuit.days_quiet() is not None else "?"))
        if pursuit.people:
            print("    %s" % ", ".join(pursuit.people))
        print("    DO: %s" % item.action)
        if item.why:
            print("    WHY: %s" % item.why)
        for note in pursuit.evidence[:2]:
            print("    \"%s\" — %s, %s" % (note.quote[:100], note.who or "?", note.date))
        print()
    hidden = len(advice) - shown
    print("%d live pursuit(s)%s." % (shown, "; %d closed or dormant (--all to see)"
                                     % hidden if hidden else ""))
    sys.stderr.write("%s\n" % llm.usage.summary())
    return 0


# --- dismiss ---------------------------------------------------------------

def cmd_dismiss(args: argparse.Namespace) -> int:
    from .dismissed import Dismissals, EMPLOYER, PERSON

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    killed = Dismissals(settings.dismissed_path)

    if args.list or (not args.who and not args.undo):
        if not killed:
            print("nothing dismissed yet")
            return 0
        for entry in killed.sorted():
            print("%-9s %s" % (entry.kind, entry.summary))
        return 0

    if args.undo:
        gone = killed.remove(args.undo)
        if gone is None:
            print("nothing dismissed matching %r" % args.undo)
            return 1
        killed.save()
        print("back in play: %s" % gone.summary)
        return 0

    if not args.reason:
        return _fail("--reason is required: in six weeks you will want to know why")
    entry = killed.add(EMPLOYER if args.employer else PERSON, args.who,
                       args.reason, label=args.who)
    killed.save()
    print("dismissed %s" % entry.summary)
    return 0


# --- ingest ----------------------------------------------------------------

IMAP_PASSWORD_ENV = "JOBSCOUT_IMAP_PASSWORD"


def cmd_ingest(args: argparse.Namespace) -> int:
    import datetime as _dt

    from . import ingest, inbox as box

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    since = None
    if args.since_years:
        since = dt.date.today() - dt.timedelta(days=int(365.25 * args.since_years))

    def progress(count: int) -> None:
        sys.stderr.write("\r  read %d…" % count)
        sys.stderr.flush()

    if args.mbox:
        me = [a for a in (args.me or "").split(",") if a.strip()]
        if not me:
            return _fail("--me is required for an mbox: without your own address "
                         "nothing can tell which messages you sent")
        messages = ingest.read_mbox(Path(args.mbox), me, since=since,
                                    on_progress=progress)
    elif args.imap:
        if not args.user:
            return _fail("--user is required for IMAP")
        password = os.environ.get(IMAP_PASSWORD_ENV, "")
        if not password:
            import getpass

            sys.stderr.write(
                "App password for %s (not your account password; create one at\n"
                "https://myaccount.google.com/apppasswords). Set %s to skip this.\n"
                % (args.user, IMAP_PASSWORD_ENV))
            password = getpass.getpass("app password: ")
        if not password:
            return _fail("no password given")
        messages = ingest.read_imap(args.host, args.user, password, since=since,
                                    limit=args.limit, on_progress=progress)
    else:
        return _fail("choose a source: --mbox PATH or --imap")

    sys.stderr.write("\r")
    if not messages:
        print("no messages found in the window")
        return 0
    path = box.save_messages(
        settings.inbox_dir / ("mail-%s.json" % dt.date.today().isoformat()), messages)
    people = len({m.counterpart for m in messages if m.counterpart})
    print("ingested %d message(s) from %d correspondent(s)" % (len(messages), people))
    print("  saved to %s" % redact(path))
    print("  now run: jobscout pursuits")
    return 0


# --- alias -----------------------------------------------------------------

def cmd_alias(args: argparse.Namespace) -> int:
    from .aliases import Aliases, infer
    from . import inbox as box, pursuits as live

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    aliases = Aliases(settings.aliases_path)

    if args.same_as:
        if not args.name:
            return _fail("give the name to keep, then --same-as the other one(s)")
        aliases.add(args.name, *args.same_as)
        aliases.save()
        print("%s also known as: %s" % (args.name, ", ".join(args.same_as)))
        return 0

    if args.suggest:
        source = settings.inbox_dir
        if not source.exists():
            return _fail("no mail ingested yet")
        messages = box.load_messages(source)
        # A relay writes every message from the same address, so its local part
        # is the same string for every recruiter alive — which proposed merging
        # three unrelated companies on the first real run.
        pairs = [(m.counterpart.partition("@")[0], live.company_of(m))
                 for m in messages
                 if live.is_human_mail(m)
                 and not any(relay in m.counterpart for relay in box.RELAY_SENDERS)
                 and not box.NOREPLY.match(m.counterpart)]
        found = infer(pairs)
        if not found:
            print("nothing to merge — no one writes to you from two employers")
            return 0
        print("The same person writes from both sides of each pair, which is what")
        print("an acquisition looks like from outside. Confirm any that are real:\n")
        for first, second in found:
            print('  jobscout alias "%s" --same-as %s' % (first, second))
        return 0

    if not len(aliases):
        print("no aliases yet")
        return 0
    for key, canonical in sorted(aliases.map.items()):
        if key != canonical.lower():
            print("%-28s -> %s" % (key, canonical))
    return 0


# --- brief -----------------------------------------------------------------

def cmd_brief(args: argparse.Namespace) -> int:
    """One screen: what to do today, and who to talk to next.

    Everything here is read from what previous runs already worked out, so it
    is instant and free. Reading the mailbox is the expensive step; looking at
    what it found should cost nothing, or you stop looking.
    """
    from . import inbox as box, network as net, pursuits as live

    settings = load_settings(require_applications=False)
    settings.ensure_data_dir()
    today = dt.date.today()
    print("Job search brief — %s\n" % today.isoformat())

    # 1. What is live, and what it needs.
    read_on, advice = live.load(settings.pursuits_path)
    if not advice:
        print("LIVE PURSUITS")
        print("  nothing read yet — run `jobscout ingest` then `jobscout pursuits`\n")
    else:
        stale = ""
        when = dt.date.fromisoformat(read_on) if read_on else None
        if when and (today - when).days > 3:
            stale = "  (read %d days ago — `jobscout pursuits` to refresh)" % (today - when).days
        acting = [a for a in advice if a.urgency in ("now", "this week")]
        print("LIVE PURSUITS — %d needing action of %d%s" % (len(acting), len(advice), stale))
        for item in acting[:args.max]:
            print("  [%s] %s" % (item.urgency, item.pursuit.label))
            print("      %s" % item.action)
        waiting = [a for a in advice if a.urgency == "none" and a.pursuit.stage != "closed"]
        if waiting:
            print("  %d more are waiting on someone else; nothing to do." % len(waiting))
        print()

    # 2. Who to talk to, from the network.
    snapshots = net.list_snapshots(settings.data_dir)
    if snapshots:
        connections = net.load_snapshot(snapshots[-1])
        affiliations, targets, names, profile = _network_context(settings)
        leads = net.rank(connections, affiliations, targets, profile,
                         conversations=_contact_history(settings),
                         killed=_killed(settings))
        leads = [l for l in leads if l.bucket != net.REST and not l.killed]
        print("PEOPLE — top of %d connection(s)" % len(connections))
        for lead in leads[:args.max]:
            print("  %-24s %s" % (lead.name[:24],
                                  ("%s @ %s" % (lead.connection.position,
                                                lead.connection.company))[:52]))
            if lead.reasons:
                print("      %s" % lead.reasons[0])
        rows = net.company_coverage(connections, targets, names)
        reachable = [name for name, people in rows if people]
        print("  %d of %d target employers have someone inside.\n"
              % (len(reachable), len(rows)))
    else:
        print("PEOPLE\n  no connections imported — `jobscout network import <export>`\n")

    # 3. Inbound approaches gone quiet.
    if settings.inbox_dir.exists():
        messages = box.load_messages(settings.inbox_dir)
        if messages:
            _affil, targets, _names, profile = _network_context(settings)
            ranked = box.follow_ups(box.outreach(messages), target_keys=targets,
                                    policy=settings.location, killed=_killed(settings))
            revivable = [f for f in ranked if f.score > 0 and not f.killed][:args.max]
            if revivable:
                print("INBOUND — recruiters who came to you and went quiet")
                for item in revivable:
                    entry = item.outreach
                    print("  %-24s %s"
                          % ((entry.person or "(name unknown)")[:24],
                             (entry.role or entry.company or "")[:52]))
                print()

    print("Next: `jobscout pursuits` to re-read the mailbox, "
          "`jobscout network leads` for the full list.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobscout",
        description="Find jobs worth applying to, from the applications you have "
                    "already written. Reads employers' own job boards, filters "
                    "location and freshness in code, and never recommends the "
                    "same role twice.")
    parser.add_argument("--version", action="version", version="jobscout %s" % __version__)
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="write .env and set up the data dir")
    init.add_argument("--applications", help="folder holding your existing applications")
    init.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                      help="where jobscout keeps its state (default: %s)" % redact(DEFAULT_DATA_DIR))
    init.add_argument("--states", default="", help="two-letter state codes you would work in, comma separated")
    init.add_argument("--cities", default="", help="cities/metros you would commute to, comma separated")
    init.add_argument("--no-remote", action="store_true", help="exclude fully remote roles")
    init.add_argument("--describe", default="", help="one line describing your location constraint")
    init.add_argument("--force", action="store_true", help="overwrite an existing .env")
    init.set_defaults(func=cmd_init)

    status = subparsers.add_parser("status", help="show configuration and what it can read")
    status.set_defaults(func=cmd_status)

    profile = subparsers.add_parser("profile", help="show (or rebuild) your inferred profile")
    profile.add_argument("--refresh", action="store_true", help="rebuild from your applications folder")
    profile.set_defaults(func=cmd_profile)

    companies = subparsers.add_parser("companies", help="list or edit the employer registry")
    companies.add_argument("--add", action="append", metavar="NAME", help="add an employer by name")
    companies.add_argument("--ignore", action="append", metavar="NAME", help="never suggest this employer")
    companies.add_argument("--reresolve", action="append", metavar="NAME",
                           help="forget the board URL and look it up again")
    companies.add_argument("--forget", action="append", metavar="NAME", help="remove an employer entirely")
    companies.set_defaults(func=cmd_companies)

    find = subparsers.add_parser("find", help="run the pipeline and print recommendations")
    find.add_argument("--expand", action="store_true",
                      help="propose more employers even if the registry is already full")
    find.add_argument("--refresh-profile", action="store_true", help="rebuild the profile first")
    find.add_argument("--max", type=int, help="how many roles to report")
    find.add_argument("--max-age", type=int, metavar="DAYS", help="freshness limit for this run")
    find.add_argument("--no-save", action="store_true", help="do not write the report file")
    find.set_defaults(func=cmd_find)

    unknown_help = "what to do when a posting does not say (include | exclude)"
    filters = subparsers.add_parser(
        "filters", help="show or set the salary / employment / clearance filters",
        description="Every filter has three outcomes — pass, fail, and DIDN'T SAY. "
                    "You choose what happens to the third, per filter.")
    filters.add_argument("--salary-min", help="e.g. 150000 or 150k")
    filters.add_argument("--salary-max", help="e.g. 250000; use 'any' to clear")
    filters.add_argument("--unknown-salary", choices=["include", "exclude"], help=unknown_help)
    filters.add_argument("--employment", metavar="TYPES",
                         help="comma separated: full-time,part-time,contract,internship")
    filters.add_argument("--unknown-employment", choices=["include", "exclude"], help=unknown_help)
    filters.add_argument("--exclude-clearance", dest="exclude_clearance",
                         action="store_true", default=None,
                         help="drop roles that require an ACTIVE security clearance")
    filters.add_argument("--allow-clearance", dest="exclude_clearance",
                         action="store_false", help="allow active-clearance roles")
    filters.add_argument("--unknown-clearance", choices=["include", "exclude"], help=unknown_help)
    filters.add_argument("--exclude-title", metavar="WORDS",
                         help="comma-separated words that disqualify a title, e.g. sales,intern")
    filters.add_argument("--require-title", metavar="WORDS",
                         help="comma-separated words a title must contain at least one of")
    filters.add_argument("--unknown-location", choices=["include", "exclude"], help=unknown_help)
    filters.add_argument("--unknown-date", choices=["include", "exclude"], help=unknown_help)
    filters.add_argument("--clear", action="store_true", help="reset every filter to its default")
    filters.set_defaults(func=cmd_filters)

    serve = subparsers.add_parser(
        "serve", help="open the local web app (no setup needed — configure it there)")
    serve.add_argument("--port", type=int, help="port to listen on (default 8765)")
    serve.add_argument("--no-browser", action="store_true", help="do not open a browser")
    serve.set_defaults(func=cmd_serve)

    history = subparsers.add_parser("history", help="what has already been recommended")
    history.add_argument("--status", choices=[RECOMMENDED, APPLIED, DISMISSED, "dropped"])
    history.add_argument("--limit", type=int, default=40)
    history.add_argument("-v", "--verbose", action="store_true", help="show rejection reasons")
    history.add_argument("--retry", action="store_true",
                         help="forget soft rejections (a page that would not load, "
                              "a verification that failed) so they are reconsidered")
    history.set_defaults(func=cmd_history)

    mark = subparsers.add_parser("mark", help="record that you applied to, or dismissed, a role")
    mark.add_argument("id", help="the id shown in the report or in `jobscout history`")
    group = mark.add_mutually_exclusive_group(required=True)
    group.add_argument("--applied", action="store_true")
    group.add_argument("--dismissed", action="store_true")
    mark.add_argument("--note", default="")
    mark.set_defaults(func=cmd_mark)

    network = subparsers.add_parser(
        "network", help="rank your own LinkedIn export for who to reach out to")
    network.add_argument("action", nargs="?", default="leads",
                         choices=["import", "leads", "coverage", "changes", "me"],
                         help="import an export, rank leads, show target-employer "
                              "coverage, diff two exports, or edit your own history")
    network.add_argument("path", nargs="?", help="the export (import only)")
    network.add_argument("--bucket", choices=list(NETWORK_BUCKETS))
    network.add_argument("--company", help="only people at this employer")
    network.add_argument("--max", type=int, default=40)
    network.add_argument("--all", action="store_true",
                         help="include connections with no signal at all")
    network.add_argument("--add", help="record an employer or school you were at")
    network.add_argument("--remove", help="forget one")
    network.add_argument("--from", dest="since", help="YYYY-MM or YYYY-MM-DD")
    network.add_argument("--to", dest="until", help="YYYY-MM; omit if you are still there")
    network.add_argument("--kind", default="employer", choices=["employer", "school"])
    network.add_argument("--from-export", dest="from_export",
                         help="read your own history from the export's Positions.csv")
    network.set_defaults(func=cmd_network)

    inbox = subparsers.add_parser(
        "inbox", help="reconstruct your search from ingested mail")
    inbox.add_argument("action", nargs="?", default="recruiters",
                       choices=["recruiters", "applications", "contacts"],
                       help="who approached you, where you applied, or who you met")
    inbox.add_argument("--path", help="a dump file or folder (default: your data dir)")
    inbox.add_argument("--max", type=int, default=25)
    inbox.add_argument("--since-years", dest="since_years", type=float,
                       default=0, help="ignore mail older than this")
    inbox.set_defaults(func=cmd_inbox)

    pursuits = subparsers.add_parser(
        "pursuits", help="what is live in your search, and what to do about each")
    pursuits.add_argument("--company", help="only this employer")
    pursuits.add_argument("--path", help="a dump file or folder")
    pursuits.add_argument("--since-years", type=float, default=2.0,
                          help="ignore mail older than this (default 2)")
    pursuits.add_argument("--all", action="store_true",
                          help="include closed and dormant pursuits")
    pursuits.set_defaults(func=cmd_pursuits)

    dismiss = subparsers.add_parser(
        "dismiss", help="rule out a person or an employer so they stop coming back")
    dismiss.add_argument("who", nargs="?",
                         help="a name, email, LinkedIn URL, or employer name")
    dismiss.add_argument("--reason", help="why — you will want this later")
    dismiss.add_argument("--employer", action="store_true",
                         help="treat the name as an employer rather than a person")
    dismiss.add_argument("--undo", help="put someone back in play")
    dismiss.add_argument("--list", action="store_true")
    dismiss.set_defaults(func=cmd_dismiss)

    ingest = subparsers.add_parser(
        "ingest", help="read your mail onto disk so the rest of the tool can use it")
    source = ingest.add_mutually_exclusive_group()
    source.add_argument("--mbox", help="an exported mailbox file (no credential needed)")
    source.add_argument("--imap", action="store_true",
                        help="read the server directly (needs an app password)")
    ingest.add_argument("--me", help="your own address(es), comma separated — mbox only")
    ingest.add_argument("--user", help="the mailbox to log into — IMAP only")
    ingest.add_argument("--host", default="imap.gmail.com")
    ingest.add_argument("--limit", type=int, default=4000,
                        help="most recent N messages at most")
    ingest.add_argument("--since-years", dest="since_years", type=float, default=2.0,
                        help="ignore mail older than this (default 2)")
    ingest.set_defaults(func=cmd_ingest)

    alias = subparsers.add_parser(
        "alias", help="tell the tool that two employer names mean one employer")
    alias.add_argument("name", nargs="?", help="the name to keep")
    alias.add_argument("--same-as", nargs="+", help="other names for it")
    alias.add_argument("--suggest", action="store_true",
                       help="propose merges from people who write from two domains")
    alias.set_defaults(func=cmd_alias)

    brief = subparsers.add_parser(
        "brief", help="one screen: what to do today, and who to talk to next")
    brief.add_argument("--max", type=int, default=6, help="rows per section")
    brief.set_defaults(func=cmd_brief)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except ConfigError as exc:
        return _fail(str(exc))
    except LLMError as exc:
        return _fail(str(exc))
    except FileNotFoundError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
