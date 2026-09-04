"""The command line.

    jobscout init --applications "~/path/to/your applications" --states NM
    jobscout status
    jobscout profile [--refresh]
    jobscout companies [--add NAME] [--ignore NAME] [--forget NAME]
    jobscout find [--expand] [--max 10]
    jobscout serve [--port 8765]
    jobscout history [--status recommended]
    jobscout mark <id> --applied | --dismissed [--note "..."]
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

# Hard filters.
JOBSCOUT_ALLOWED_STATES={states}
JOBSCOUT_ALLOWED_CITIES={cities}
JOBSCOUT_ALLOW_REMOTE={remote}
JOBSCOUT_ALLOW_HYBRID=false
JOBSCOUT_MAX_AGE_DAYS=30

# Per-run budget. Every one of these is a billed model call.
JOBSCOUT_COMPANY_TARGET=30
JOBSCOUT_MAX_RESOLVE_PER_RUN=10
JOBSCOUT_MAX_SCANS_PER_RUN=12
JOBSCOUT_MAX_VERIFY_PER_RUN=20
JOBSCOUT_MAX_RESULTS=10
JOBSCOUT_MAX_WORKERS=4

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
        weights_summary=settings.weights().describe())
    print(text)
    if not args.no_save:
        path = report.write(text, settings.reports_dir)
        sys.stderr.write("\nreport saved to %s\n" % redact(path))
    sys.stderr.write("%s\n" % result.usage_summary)
    return 0


# --- serve -----------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    settings = load_settings()
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
    init.add_argument("--states", default="", help="comma-separated state codes you will work in, e.g. NM")
    init.add_argument("--cities", default="", help="comma-separated cities/metros, e.g. 'Albuquerque,Santa Fe'")
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

    serve = subparsers.add_parser("serve", help="open the local web app")
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
