"""A local web app for working through the board.

``jobscout serve`` starts a small server on localhost and opens a browser. It is
built on the standard library — no framework, no CDN, nothing that phones home —
because the page is looking at your job search and that should not leave your
machine.

What it adds over the Markdown report:

* the board persists between runs, so it is a list you work through rather than
  a wall of text you re-read
* roles are ranked by **percentile** against everything ever scored for you
* the fit / likelihood / recency weights are sliders, and moving one re-ranks
  the whole board instantly, with no model calls — the scores are already stored
* you can mark a role applied or dismissed, which writes straight to the history
  so the next run stops offering it
* you can start a run from the page and watch the log as it happens
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import filters as hard_filters
from . import pipeline, scoring
from .board import Board
from .companies import Registry
from .config import (ConfigError, DEFAULT_DATA_DIR, LocationPolicy, Settings,
                     load_requirements, save_location_policy, save_requirements,
                     _assert_outside_repo)
from .corpus import load_corpus
from .history import APPLIED, DISMISSED, History, RECOMMENDED
from .models import Posting

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _as_words(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


class RunState:
    """One run at a time, with its log captured for the browser."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.log: List[str] = []
        self.finished_at: Optional[str] = None
        self.error: str = ""
        self.summary: str = ""

    def append(self, message: str) -> None:
        with self.lock:
            self.log.append(message)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {"running": self.running, "log": list(self.log),
                    "finished_at": self.finished_at, "error": self.error,
                    "summary": self.summary}


class App:
    """Everything the page can see or change.

    Nothing has to be configured before the server starts. You point it at a
    folder in the browser, set the filters there, and run — and the saved
    employer list, profile and history are *opt-in*, not assumed, so a fresh
    search is genuinely fresh rather than quietly shaped by an older one.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = RunState()
        #: Reuse the saved employers / profile / history? Off until chosen.
        self.use_saved = False
        self.session_dir: Optional[Path] = None

    # --- configuration ----------------------------------------------------
    def configured(self) -> bool:
        return self.settings.applications_dir.is_dir()

    def browse(self, raw: str) -> Dict[str, Any]:
        """List sub-folders of a path, so the folder can be picked in the page."""
        text = (raw or "~").strip() or "~"
        path = Path(os.path.expanduser(text))
        try:
            path = path.resolve()
        except OSError:
            return {"error": "cannot read %s" % text, "path": text, "entries": []}
        if not path.is_dir():
            return {"error": "not a folder: %s" % path, "path": str(path), "entries": []}

        entries = []
        try:
            for child in sorted(path.iterdir()):
                if child.name.startswith(".") or not child.is_dir():
                    continue
                entries.append({"name": child.name, "path": str(child)})
        except PermissionError:
            return {"error": "no permission to read %s" % path,
                    "path": str(path), "entries": []}
        return {
            "path": str(path),
            "parent": str(path.parent) if path.parent != path else "",
            "entries": entries[:400],
            "inspection": self.inspect(str(path)),
        }

    def inspect(self, raw: str) -> Dict[str, Any]:
        """What jobscout would read from a folder, before committing to it."""
        path = Path(os.path.expanduser((raw or "").strip()))
        if not path.is_dir():
            return {"ok": False, "note": "not a folder"}
        try:
            _assert_outside_repo(path, "applications folder")
        except ConfigError as exc:
            return {"ok": False, "note": str(exc).splitlines()[0]}
        try:
            corpus = load_corpus(path)
        except Exception as exc:
            return {"ok": False, "note": str(exc)[:160]}
        if not corpus.documents:
            return {"ok": False,
                    "note": "no readable documents here (PDF, DOCX, TXT or MD)"}
        return {
            "ok": True,
            "documents": len(corpus.documents),
            "companies": corpus.company_names(),
            "note": "%d document(s) across %d application folder(s)"
                    % (len(corpus.documents), len(corpus.applications)),
        }

    def configure(self, data: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.settings

        if data.get("applications_dir"):
            path = Path(os.path.expanduser(str(data["applications_dir"]).strip()))
            if not path.is_dir():
                return {"ok": False, "error": "no such folder: %s" % path}
            try:
                _assert_outside_repo(path, "applications folder")
            except ConfigError as exc:
                return {"ok": False, "error": str(exc)}
            settings.applications_dir = path

        if data.get("data_dir"):
            data_dir = Path(os.path.expanduser(str(data["data_dir"]).strip()))
            try:
                _assert_outside_repo(data_dir, "data folder")
            except ConfigError as exc:
                return {"ok": False, "error": str(exc)}
            settings.data_dir = data_dir

        if "use_saved" in data:
            self.use_saved = bool(data["use_saved"])

        location = data.get("location")
        if isinstance(location, dict):
            policy = settings.location
            if "states" in location:
                policy.allowed_states = _as_words(location["states"])
            if "cities" in location:
                policy.allowed_cities = _as_words(location["cities"])
            if "allow_remote" in location:
                policy.allow_remote = bool(location["allow_remote"])
            if "allow_hybrid" in location:
                policy.allow_hybrid = bool(location["allow_hybrid"])
            policy.description = ""      # rebuild it from the parts
            settings.location = policy.normalized()

        if data.get("max_age_days"):
            try:
                settings.max_age_days = max(1, int(data["max_age_days"]))
            except (TypeError, ValueError):
                pass
        if data.get("max_scans"):
            try:
                settings.max_scans_per_run = max(1, int(data["max_scans"]))
            except (TypeError, ValueError):
                pass

        if data.get("remember"):
            settings.ensure_data_dir()
            save_location_policy(settings.data_dir, settings.location)
            save_requirements(settings.data_dir, settings.requirements)
        return {"ok": True, "configured": self.configured()}

    def run_settings(self) -> Settings:
        """The settings a run should use, honouring the opt-in cache.

        Without the cache the run gets its own folder, so a previous search's
        employer list, profile and "already seen" history cannot shape it.
        """
        if self.use_saved:
            return self.settings
        if self.session_dir is None:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            self.session_dir = self.settings.data_dir / "runs" / stamp
            self.session_dir.mkdir(parents=True, exist_ok=True)
        fresh = replace(self.settings, data_dir=self.session_dir)
        return fresh

    def view_settings(self) -> Settings:
        """Where the page reads from — the same place the last run wrote to."""
        if not self.use_saved and self.session_dir is not None:
            return replace(self.settings, data_dir=self.session_dir)
        return self.settings

    # --- data ------------------------------------------------------------
    def board_payload(self) -> Dict[str, Any]:
        settings = self.view_settings()
        board = Board(settings.board_path)
        history = History(settings.history_path)
        weights = settings.weights()

        statuses = {e.id: e.status for e in history.entries}
        postings = board.postings()
        # Re-score live: the sliders change the weights, not the stored fit and
        # likelihood, so ranking is instant and costs nothing.
        ranked = scoring.score_all(postings, weights,
                                   baseline=history.scored_composites())
        roles = []
        for posting in ranked:
            record = posting.to_dict()
            record["status"] = statuses.get(posting.id, RECOMMENDED)
            record["age_days"] = posting.age_days()
            record["band"] = scoring.band(posting.percentile, posting.composite)
            stored = board.items.get(posting.id, {})
            record["first_seen"] = stored.get("first_seen", "")
            record["stage"] = stored.get("stage", posting.stage)
            # Re-check the current requirements on every read, so moving a
            # filter shows its effect at once instead of only on the next run.
            ok, why = settings.requirements.check(posting)
            # The location policy is re-checked here too, so moving it shows its
            # effect on the board at once rather than only on the next run.
            in_area, mode, where = hard_filters.check_location(posting, settings.location)
            record["work_mode"] = mode
            record["requirement_ok"] = ok and in_area
            record["requirement_reason"] = why if ok else why
            if not in_area:
                record["requirement_reason"] = where
            roles.append(record)

        profile: Dict[str, Any] = {}
        if settings.profile_path.exists():
            try:
                profile = json.loads(settings.profile_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                profile = {}

        registry = Registry(settings.companies_path)
        return {
            "roles": roles,
            "weights": {"fit": weights.fit, "likelihood": weights.likelihood,
                        "recency": weights.recency,
                        "halflife": weights.halflife_days},
            "requirements": dict(settings.requirements.to_dict(),
                                 summary=settings.requirements.summary()),
            "location": {"summary": settings.location.summary(),
                         "states": settings.location.allowed_states,
                         "cities": settings.location.allowed_cities,
                         "remote": settings.location.allow_remote,
                         "hybrid": settings.location.allow_hybrid,
                         "max_age_days": settings.max_age_days},
            "profile": {"headline": profile.get("headline", ""),
                        "seniority": profile.get("seniority", ""),
                        "target_titles": profile.get("target_titles", [])[:8],
                        "generated": profile.get("generated", "")},
            "counts": {
                "roles": len(roles),
                "applied": len(history.by_status(APPLIED)),
                "dismissed": len(history.by_status(DISMISSED)),
                "evaluated": len(history.entries),
                "employers": len(registry.companies),
                "boards_known": len([c for c in registry.active() if c.careers_url]),
            },
            "companies": [
                {"name": c.name, "status": c.status, "url": c.careers_url,
                 "ats": c.ats, "why": c.why, "presence": c.presence,
                 "last_scanned": c.last_scanned, "found": c.postings_found}
                for c in registry.sorted()
            ],
            "run": self.state.snapshot(),
            "backend": settings.backend,
            "configured": self.configured(),
            "applications_dir": str(settings.applications_dir)
                                if self.configured() else "",
            "applications": self.inspect(str(settings.applications_dir))
                            if self.configured() else {"ok": False, "note": ""},
            "data_dir": str(settings.data_dir),
            "use_saved": self.use_saved,
            "max_scans": settings.max_scans_per_run,
            "default_data_dir": str(DEFAULT_DATA_DIR),
        }

    # --- actions ---------------------------------------------------------
    def start_run(self, expand: bool = False) -> Dict[str, Any]:
        if not self.configured():
            return {"started": False,
                    "reason": "choose the folder holding your existing applications first"}
        with self.state.lock:
            if self.state.running:
                return {"started": False, "reason": "a run is already in progress"}
            self.state.running = True
            self.state.log = []
            self.state.error = ""
            self.state.summary = ""
            self.state.finished_at = None

        def publish(postings) -> None:
            """Write roles to the board the moment the pipeline finds them."""
            board = Board(self.run_settings().board_path)
            board.merge(postings)
            board.save()

        settings = self.run_settings()

        def work() -> None:
            pipeline.set_logger(self.state.append)
            try:
                if not self.use_saved:
                    self.state.append(
                        "running WITHOUT the saved cache — fresh profile, fresh "
                        "employer list, no history of what you have already seen")
                    self.state.append("this run's data: %s" % settings.data_dir)
                result = pipeline.find(settings, expand=expand,
                                       on_update=publish)
                self.state.summary = ("%d new role(s) · %s"
                                      % (len(result.recommended), result.usage_summary))
                self.state.append("done — " + self.state.summary)
            except Exception as exc:
                self.state.error = str(exc)
                self.state.append("failed: %s" % exc)
            finally:
                pipeline.set_logger(None)
                with self.state.lock:
                    self.state.running = False
                    self.state.finished_at = dt.datetime.now().strftime("%H:%M:%S")

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    def mark(self, posting_id: str, status: str) -> Dict[str, Any]:
        if status not in (APPLIED, DISMISSED, RECOMMENDED):
            return {"ok": False, "reason": "unknown status"}
        history = History(self.view_settings().history_path)
        entry = history.mark(posting_id, status)
        return {"ok": entry is not None}

    def set_requirements(self, data: Dict[str, Any]) -> Dict[str, Any]:
        from .requirements import EXCLUDE, INCLUDE, Requirements

        current = self.settings.requirements
        fields = set(Requirements.__dataclass_fields__)  # type: ignore[attr-defined]
        for key, value in data.items():
            if key not in fields:
                continue
            if key in ("salary_min", "salary_max"):
                try:
                    value = int(value) if str(value).strip() not in ("", "None") else None
                except (TypeError, ValueError):
                    continue
            elif key.startswith("unknown_"):
                if str(value).lower() not in (INCLUDE, EXCLUDE):
                    continue
            elif key.endswith("_types") or key.endswith("_words"):
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                elif not isinstance(value, list):
                    continue
            elif key == "exclude_clearance_required":
                value = bool(value)
            setattr(current, key, value)
        self.settings.requirements = current.normalized()
        self.settings.ensure_data_dir()
        save_requirements(self.settings.data_dir, self.settings.requirements)
        return {"ok": True, "summary": self.settings.requirements.summary()}

    def set_weights(self, data: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.settings
        for key, attr in (("fit", "weight_fit"), ("likelihood", "weight_likelihood"),
                          ("recency", "weight_recency"),
                          ("halflife", "recency_halflife_days")):
            if key in data:
                try:
                    setattr(settings, attr, float(data[key]))
                except (TypeError, ValueError):
                    pass
        # Persist so the CLI and the next `serve` agree with what you set here.
        settings.ensure_data_dir()
        (settings.data_dir / "weights.json").write_text(
            json.dumps({"fit": settings.weight_fit,
                        "likelihood": settings.weight_likelihood,
                        "recency": settings.weight_recency,
                        "halflife_days": settings.recency_halflife_days},
                       indent=2) + "\n", encoding="utf-8")
        return {"ok": True}


def load_saved_weights(settings: Settings) -> None:
    """Pick up weights the web UI saved, unless the environment overrode them."""
    path = settings.data_dir / "weights.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    import os

    if "JOBSCOUT_WEIGHT_FIT" not in os.environ:
        settings.weight_fit = float(data.get("fit", settings.weight_fit))
        settings.weight_likelihood = float(data.get("likelihood", settings.weight_likelihood))
        settings.weight_recency = float(data.get("recency", settings.weight_recency))
        settings.recency_halflife_days = float(
            data.get("halflife_days", settings.recency_halflife_days))


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "jobscout"

        def log_message(self, fmt, *args):  # quieter than the default
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                page = (STATIC_DIR / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(app.board_payload())
            elif path == "/api/run":
                self._json(app.state.snapshot())
            elif path == "/api/browse":
                query = parse_qs(urlparse(self.path).query)
                self._json(app.browse((query.get("path") or ["~"])[0]))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                data = {}
            if path == "/api/run":
                self._json(app.start_run(expand=bool(data.get("expand"))))
            elif path == "/api/mark":
                self._json(app.mark(str(data.get("id") or ""),
                                    str(data.get("status") or "")))
            elif path == "/api/weights":
                self._json(app.set_weights(data))
            elif path == "/api/requirements":
                self._json(app.set_requirements(data))
            elif path == "/api/configure":
                self._json(app.configure(data))
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(settings: Settings, port: Optional[int] = None,
          open_browser: bool = True) -> int:
    load_saved_weights(settings)
    app = App(settings)
    port = port or settings.port
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = "http://127.0.0.1:%d/" % port
    print("jobscout is running at %s" % url)
    print("press Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
