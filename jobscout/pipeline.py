"""The pipeline that wires the agents and the filters together.

    profile
      -> propose employers who fit you and your geography
      -> resolve each employer's real careers board (once, then remembered)
      -> read those boards
      -> HARD FILTERS: source, location, freshness, already-seen
      -> verify each survivor by fetching its page
      -> HARD FILTERS again, against what the page actually said
      -> rank what is left

Every stage that costs money is capped per run, and every posting that is dropped
is written to the history with its reason, so the next run neither repeats the
recommendation nor pays to re-check the rejection.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import agents, fetchers, filters, requirements, scoring, sources
from .board import Board
from .companies import Company, NO_BOARD, RESOLVED, Registry
from .config import Settings
from .corpus import Corpus, load_corpus
from .history import DROPPED, History, RECOMMENDED
from .llm import LLM, LLMError
from .models import Posting


def _stderr_logger(message: str) -> None:
    """Progress goes to stderr so `jobscout find > report.md` stays clean."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


#: Swapped by the web app so a browser can watch a run happen.
_logger: Callable[[str], None] = _stderr_logger


def set_logger(logger: Optional[Callable[[str], None]]) -> None:
    global _logger
    _logger = logger or _stderr_logger


def _log(message: str) -> None:
    _logger(message)


@dataclass
class RunResult:
    recommended: List[Posting] = field(default_factory=list)
    dropped: List[Posting] = field(default_factory=list)
    #: Not rejected — just past a per-run budget. Deliberately NOT written to the
    #: history, so the next run picks them up instead of burying them.
    deferred: List[Posting] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    profile: Dict[str, Any] = field(default_factory=dict)
    usage_summary: str = ""
    report_path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)


#: How often to say "still working" while slow tasks are in flight. Long agent
#: scans can run for minutes, and silence is indistinguishable from a hang.
HEARTBEAT_SECONDS = 20


class _Progress:
    """Live, ordered progress for work happening on several threads at once."""

    def __init__(self, stage: str, total: int) -> None:
        self.stage = stage
        self.total = total
        self.done = 0
        self.started_at = time.time()
        self.in_flight: Dict[str, float] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _counter(self) -> str:
        return "[%*d/%d]" % (len(str(self.total)), self.done, self.total)

    def start(self, name: str) -> None:
        # No counter on a start line: the count means "finished", and showing it
        # beside a job that is only beginning reads as though it were done.
        with self.lock:
            self.in_flight[name] = time.time()
            _log("      → %s %s…" % (self.stage, name))

    def finish(self, name: str, detail: str) -> None:
        with self.lock:
            began = self.in_flight.pop(name, time.time())
            self.done += 1
            _log("  %s %-34s %s (%.1fs)"
                 % (self._counter(), name[:34], detail, time.time() - began))

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            with self.lock:
                if not self.in_flight:
                    continue
                waiting = sorted(self.in_flight.items(), key=lambda kv: kv[1])
                names = ", ".join("%s (%.0fs)" % (n, time.time() - t)
                                  for n, t in waiting[:3])
                more = "" if len(waiting) <= 3 else " +%d more" % (len(waiting) - 3)
                _log("      … still working: %s%s" % (names, more))

    def __enter__(self) -> "_Progress":
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        _log("  %s %s finished in %.0fs"
             % (self._counter(), self.stage, time.time() - self.started_at))


def _parallel(items: Sequence[Any], worker: Callable[[Any], Any],
              max_workers: int, stage: str = "",
              label: Optional[Callable[[Any], str]] = None,
              detail: Optional[Callable[[Any], str]] = None
              ) -> List[Tuple[Any, Any, Optional[Exception]]]:
    """Run ``worker`` over ``items``; never let one failure kill the run.

    When ``stage`` is given, each item is announced as it starts and again as it
    finishes, with a heartbeat naming whatever is still in flight — so a long
    agent scan reads as slow rather than as a hang.
    """
    results: List[Tuple[Any, Any, Optional[Exception]]] = []
    if not items:
        return results

    name_of = label or (lambda item: str(item))

    if not stage:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {pool.submit(worker, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    results.append((item, future.result(), None))
                except Exception as exc:
                    results.append((item, None, exc))
        return results

    with _Progress(stage, len(items)) as progress:
        def run(item: Any) -> Any:
            progress.start(name_of(item))
            return worker(item)

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = {pool.submit(run, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    outcome = future.result()
                    results.append((item, outcome, None))
                    note = detail(outcome) if detail else "done"
                except Exception as exc:  # one bad board should not sink the run
                    results.append((item, None, exc))
                    note = "failed: %s" % str(exc)[:80]
                progress.finish(name_of(item), note)
    return results


# --- profile ---------------------------------------------------------------

def load_or_build_profile(settings: Settings, llm: LLM, corpus: Corpus,
                          refresh: bool = False) -> Dict[str, Any]:
    path = settings.profile_path
    if path.exists() and not refresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            _log("profile.json was unreadable; rebuilding it")
    _log("reading %d document(s) from your applications folder…" % len(corpus.documents))
    profile = agents.build_profile(llm, corpus)
    settings.ensure_data_dir()
    path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    _log("built profile: %s" % profile.get("headline", "(no headline)"))
    return profile


# --- employers -------------------------------------------------------------

def expand_registry(settings: Settings, llm: LLM, registry: Registry,
                    profile: Dict[str, Any], force: bool = False) -> int:
    """Ask for more employers if the registry is short of its target."""
    active = registry.active()
    if len(active) >= settings.company_target and not force:
        return 0
    want = settings.propose_batch if force else min(
        settings.propose_batch, settings.company_target - len(active))
    _log("proposing %d employer(s) that fit your background and location…" % want)
    proposed = agents.propose_companies(
        llm, profile, settings.location, registry.known_names(), count=want)
    added = 0
    for company in proposed:
        before = len(registry.companies)
        registry.add(company)
        if len(registry.companies) > before:
            added += 1
    registry.save()
    _log("added %d new employer(s) — registry now: %s" % (added, registry.summary()))
    return added


def resolve_boards(settings: Settings, llm: LLM, registry: Registry) -> int:
    """Find the careers board for employers that do not have one yet."""
    pending = registry.needing_resolution()[:settings.max_resolve_per_run]
    if not pending:
        return 0
    _log("finding careers boards for %d employer(s)…" % len(pending))
    resolved = 0
    for company, result, error in _parallel(
            pending, lambda c: agents.resolve_board(llm, c), settings.max_workers,
            stage="looking up", label=lambda c: c.name,
            detail=lambda r: (("found %s" % (r or {}).get("careers_url", ""))[:70]
                              if (r or {}).get("careers_url") else "no board found")):
        if error is not None:
            company.note = "board lookup failed: %s" % str(error)[:120]
            continue
        url = sources.clean_board_url((result or {}).get("careers_url", ""))
        if url:
            ok, source_class, reason = sources.check_source(url, company.name)
            if not ok:
                # A careers page on a random host is not one we will read.
                company.status = NO_BOARD
                company.note = "rejected board URL (%s)" % reason
                continue
            registry.mark_resolved(company, url, (result or {}).get("ats", ""))
            company.note = (result or {}).get("note", "")
            resolved += 1
        else:
            company.status = NO_BOARD
            company.note = (result or {}).get("note", "no board found")
    registry.save()
    _log("resolved %d board(s)" % resolved)
    return resolved


def _scan_one(settings: Settings, llm: LLM, company: Company,
              profile: Dict[str, Any]) -> Tuple[List[Posting], str, int]:
    """Read one employer's board, preferring its API over an agent.

    Returns ``(postings, how, narrowed)``. An ATS API returns the *whole* board,
    so the free location filter and a title-overlap trim run here, before the
    postings reach anything that costs money.
    """
    titles = list(profile.get("target_titles") or []) + \
        list(profile.get("adjacent_titles") or [])
    direct = fetchers.fetch(company.name, company.careers_url,
                            context={"titles": titles})
    if direct is not None and direct.ok:
        raw = direct.postings
        in_area = []
        for posting in raw:
            accepted, mode, _reason = filters.check_location(posting, settings.location)
            if not accepted:
                continue
            posting.work_mode = mode
            # Salary, employment type and clearance are checked here too — an
            # API hands over the whole board, so these cost nothing at all.
            wanted, _why = settings.requirements.check(posting)
            if wanted:
                in_area.append(posting)
        kept, trimmed = filters.narrow_to_relevant(
            in_area, profile, settings.max_postings_per_company)
        narrowed = (len(raw) - len(in_area)) + trimmed
        for posting in kept:
            # A role returned by the board's live API is, by definition, on the
            # board right now. There is nothing for a verification fetch to add,
            # so the budget is saved for postings an agent reported.
            posting.verified = "live"
            posting.verification_note = ("listed on %s's live board"
                                         % (direct.ats or "the employer"))
        return kept, "%s API" % (direct.ats or "board"), narrowed

    # No API for this host (or the API failed): fall back to the agent.
    if direct is not None and not direct.ok:
        company.note = direct.note
    found = agents.scan_board(llm, company, profile, settings.location,
                              settings.max_age_days)
    return found, "agent", 0


def scan_boards(settings: Settings, llm: LLM, registry: Registry,
                profile: Dict[str, Any],
                on_company: Optional[Callable[[Any, List[Posting]], None]] = None
                ) -> Tuple[List[Posting], List[str], Dict[str, int]]:
    """Read the boards we know about and collect what is on them.

    Boards with an API are read EVERY run and are never capped: they are free
    and take under a second, so throttling them only means missing jobs. The cap
    and the rescan interval apply solely to the slow agent-driven scans.
    """
    everything = registry.active()
    api_boards = [c for c in everything
                  if c.careers_url and fetchers.supports(c.careers_url)]
    agent_boards = [c for c in registry.scannable(settings.rescan_after_days)
                    if c.careers_url and not fetchers.supports(c.careers_url)]
    targets = api_boards + agent_boards[:settings.max_scans_per_run]
    stats: Dict[str, int] = {}
    if not targets:
        return [], [], stats
    _log("reading %d job board(s) — %d free via an ATS API, %d slower agent scan(s)%s"
         % (len(targets), len(api_boards), min(len(agent_boards), settings.max_scans_per_run),
            "" if len(agent_boards) <= settings.max_scans_per_run
            else " (%d more agent board(s) left for next run)"
                 % (len(agent_boards) - settings.max_scans_per_run)))
    postings: List[Posting] = []
    errors: List[str] = []
    narrowed_total = 0
    via_api = 0
    for company, outcome, error in _parallel(
            targets, lambda c: _scan_one(settings, llm, c, profile),
            settings.max_workers, stage="reading", label=lambda c: c.name,
            detail=lambda o: "%d role(s) via %s" % (len(o[0]), o[1])):
        if error is not None:
            errors.append("%s: %s" % (company.name, str(error)[:160]))
            continue
        found, how, narrowed = outcome
        narrowed_total += narrowed
        if how.endswith("API"):
            via_api += 1
        registry.mark_scanned(company, len(found))
        postings.extend(found)
        if on_company is not None:
            # Hand this employer's roles on immediately, so a dashboard fills in
            # while the run continues instead of staying blank until the end.
            on_company(company, found)
    registry.save()
    stats["boards_read"] = len(targets)
    stats["boards_via_api"] = via_api
    stats["boards_skipped_slow"] = max(0, len(agent_boards) - settings.max_scans_per_run)
    stats["narrowed_at_source"] = narrowed_total
    _log("found %d relevant posting(s) across %d board(s) (%d read directly via "
         "an ATS API; %d off-location or off-target roles skipped for free)"
         % (len(postings), len(targets), via_api, narrowed_total))
    return postings, errors, stats


# --- filtering -------------------------------------------------------------

def _drop(posting: Posting, reason: str, dropped: List[Posting],
          stats: Dict[str, int], bucket: str) -> None:
    posting.rejected_reason = reason
    dropped.append(posting)
    stats[bucket] = stats.get(bucket, 0) + 1


def prefilter(postings: Sequence[Posting], settings: Settings, history: History,
              corpus: Optional[Corpus] = None, today: Optional[dt.date] = None,
              seen_ids: Optional[set] = None, seen_pairs: Optional[set] = None
              ) -> Tuple[List[Posting], List[Posting], Dict[str, int]]:
    """Everything we can decide before spending a fetch on verification.

    Called once per employer so results can stream out as they are found; the
    two ``seen`` sets carry deduplication across those calls.
    """
    kept: List[Posting] = []
    dropped: List[Posting] = []
    stats: Dict[str, int] = {"raw": len(postings)}
    applied_keys = corpus.company_keys() if corpus else set()

    # Source first, THEN dedupe. The other order lets an aggregator's copy of a
    # role absorb the employer's own listing and take the whole role down with it.
    trusted: List[Posting] = []
    for posting in postings:
        ok, _source_class, reason = sources.check_source(posting.url, posting.company)
        if not ok:
            _drop(posting, "untrusted source: %s" % reason, dropped, stats, "dropped_source")
            continue
        trusted.append(posting)

    for posting in filters.dedupe(trusted, seen_ids, seen_pairs):
        if filters.excluded_company(posting, settings.exclude_companies):
            _drop(posting, "excluded employer", dropped, stats, "dropped_excluded")
            continue
        seen, why = history.seen_before(posting, today)
        if seen:
            _drop(posting, why, dropped, stats, "dropped_seen")
            continue
        ok, mode, reason = filters.check_location(posting, settings.location)
        posting.work_mode = mode
        if not ok:
            # "It does not say" is not the same as "no", so the unknown policy
            # decides, not the filter.
            if reason == "no location stated" and \
                    settings.requirements.unknown_location == requirements.INCLUDE:
                pass
            else:
                _drop(posting, reason, dropped, stats, "dropped_location")
                continue
        # Freshness with the date the board claimed; re-checked after verification.
        if posting.posted_date is not None:
            fresh, why = filters.check_freshness(posting, settings.max_age_days, today)
            if not fresh:
                _drop(posting, why, dropped, stats, "dropped_stale")
                continue
        elif settings.requirements.unknown_date == requirements.EXCLUDE \
                and posting.verified != "live":
            _drop(posting, "no post date, and you asked to drop undated postings",
                  dropped, stats, "dropped_undated")
            continue

        accepted, why = settings.requirements.check(posting)
        if not accepted:
            _drop(posting, why, dropped, stats, "dropped_requirements")
            continue
        if why:
            posting.summary = (posting.summary + "\n\n" + why).strip() \
                if posting.summary else why
        if posting.company_key in applied_keys:
            # The one thing worth skipping: you have already applied here, and
            # the evidence is sitting in your applications folder.
            _drop(posting, "you already applied to this employer",
                  dropped, stats, "dropped_already_applied")
            continue
        kept.append(posting)

    stats["survived_prefilter"] = len(kept)
    return kept, dropped, stats


def verify(settings: Settings, llm: LLM, postings: Sequence[Posting],
           today: Optional[dt.date] = None
           ) -> Tuple[List[Posting], List[Posting], List[Posting], Dict[str, int]]:
    """Fetch each posting and re-run the hard filters against the real page.

    Returns ``(verified, dropped, deferred, stats)``.
    """
    # Anything already known-live came straight from an employer's board API and
    # needs no fetch; only agent-reported postings are spent budget on.
    already_live = [p for p in postings if p.verified == "live"]
    needs_check = [p for p in postings if p.verified != "live"]
    targets = needs_check[:settings.max_verify_per_run]
    deferred = needs_check[settings.max_verify_per_run:]
    kept: List[Posting] = list(already_live)
    dropped: List[Posting] = []
    stats: Dict[str, int] = {"live_from_api": len(already_live)}
    if deferred:
        stats["deferred_budget"] = len(deferred)
    if not targets:
        stats["verified"] = len(kept)
        return kept, dropped, deferred, stats

    _log("verifying %d agent-reported posting(s) by fetching each listing "
         "(%d already confirmed live by a board API)…"
         % (len(targets), len(already_live)))
    for posting, result, error in _parallel(
            targets, lambda p: agents.verify_posting(llm, p), settings.max_workers,
            stage="checking", label=lambda p: "%s — %s" % (p.company, p.title[:40]),
            detail=lambda r: (r or {}).get("status", "unknown")):
        if error is not None:
            posting.verified = "unchecked"
            posting.verification_note = str(error)[:200]
        else:
            result = result or {}
            posting.verified = {"live": "live", "dead": "dead", "mismatch": "mismatch"}.get(
                result.get("status", ""), "unchecked")
            posting.verification_note = result.get("note", "")
            # The page beats the board summary on both of the fields that matter.
            if result.get("actual_location"):
                posting.location = result["actual_location"]
            if result.get("posted"):
                posting.posted = result["posted"]
            if result.get("actual_title") and posting.verified == "live":
                posting.title = result["actual_title"] or posting.title

        ok, reason = filters.check_verified(posting)
        if not ok:
            _drop(posting, reason, dropped, stats, "dropped_unverified")
            continue
        ok, mode, reason = filters.check_location(posting, settings.location)
        posting.work_mode = mode
        if not ok:
            _drop(posting, "on the live page: %s" % reason, dropped, stats,
                  "dropped_location_verified")
            continue
        fresh, reason = filters.check_freshness(posting, settings.max_age_days, today)
        if not fresh:
            _drop(posting, reason, dropped, stats, "dropped_stale_verified")
            continue
        kept.append(posting)

    stats["verified"] = len(kept)
    return kept, dropped, deferred, stats


# --- the whole run ---------------------------------------------------------

def find(settings: Settings, *, refresh_profile: bool = False,
         expand: bool = False, today: Optional[dt.date] = None,
         on_update: Optional[Callable[[List[Posting]], None]] = None) -> RunResult:
    """Run the pipeline.

    ``on_update`` is called with postings as soon as each stage reaches them —
    found, then verified, then scored — so a live view can show a role the
    moment it is discovered rather than after every employer has been read.
    """
    settings.ensure_data_dir()
    today = today or dt.date.today()
    result = RunResult()

    llm = LLM.from_settings(settings)
    corpus = load_corpus(settings.applications_dir)
    profile = load_or_build_profile(settings, llm, corpus, refresh=refresh_profile)
    result.profile = profile

    registry = Registry(settings.companies_path)
    history = History(settings.history_path)

    expand_registry(settings, llm, registry, profile, force=expand)
    resolve_boards(settings, llm, registry)
    # Filter each employer's board the moment it is read, so survivors can be
    # published straight away rather than waiting on the slowest board.
    seen_ids: set = set()
    seen_pairs: set = set()
    kept: List[Posting] = []

    def handle(company: Any, found: List[Posting]) -> None:
        survivors, dropped_here, stats_here = prefilter(
            found, settings, history, corpus, today, seen_ids, seen_pairs)
        kept.extend(survivors)
        result.dropped.extend(dropped_here)
        for key, value in stats_here.items():
            result.stats[key] = result.stats.get(key, 0) + value
        if on_update and survivors:
            for posting in survivors:
                posting.stage = "found"
            on_update(survivors)

    _raw, scan_errors, scan_stats = scan_boards(
        settings, llm, registry, profile, on_company=handle)
    result.errors.extend(scan_errors)
    result.stats.update(scan_stats)

    kept, dropped, deferred, verify_stats = verify(settings, llm, kept, today)
    result.dropped.extend(dropped)
    result.deferred.extend(deferred)
    result.stats.update(verify_stats)
    if on_update and kept:
        for posting in kept:
            posting.stage = "verified"
        on_update(kept)

    if kept:
        _log("scoring %d surviving role(s)…" % len(kept))
        try:
            scores = agents.rank_postings(llm, kept, profile)
        except LLMError as exc:
            scores = {}
            result.errors.append("ranking failed: %s" % exc)
        for posting in kept:
            score = scores.get(posting.id) or {}
            posting.fit_score = int(score.get("fit_score") or 0)
            posting.likelihood = int(score.get("likelihood") or 0)
            posting.fit_rationale = score.get("rationale", "")
            posting.likelihood_rationale = score.get("odds", "")
            posting.resembles = score.get("resembles", "")
            if score.get("concerns"):
                posting.concerns = score["concerns"]
            if score.get("angle"):
                posting.summary = (posting.summary + "\n\nLead with: " + score["angle"]).strip()

    # Blend fit, likelihood and recency, then percentile-rank the result against
    # every role ever scored for this candidate.
    kept = scoring.score_all(kept, settings.weights(),
                             baseline=history.scored_composites(), today=today)
    for posting in kept:
        posting.stage = "scored"
    if on_update and kept:
        on_update(kept)
    result.recommended = kept[:settings.max_results]
    # Verified, in-location roles that simply did not fit in this report are held
    # over rather than recorded, so tomorrow's run can still surface them.
    result.deferred.extend(kept[settings.max_results:])
    result.stats["recommended"] = len(result.recommended)
    result.stats["held_over"] = len(result.deferred)
    result.stats["employers_known"] = len(registry.companies)

    # The board keeps the full detail of everything worth acting on, so the web
    # app and later runs have something richer than the history's one-liners.
    board = Board(settings.board_path)
    board.merge(result.recommended + result.deferred, today=today)
    board.save()

    # Only decided outcomes go in the history: what we recommended, and what we
    # ruled out (with the reason). Held-over roles are left undecided on purpose.
    for posting in result.recommended:
        history.record(posting, RECOMMENDED, today=today)
    for posting in result.dropped:
        history.record(posting, DROPPED, posting.rejected_reason, today=today)

    result.usage_summary = llm.usage.summary()
    return result
