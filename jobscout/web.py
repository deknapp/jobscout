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
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import pipeline, scoring
from .board import Board
from .companies import Registry
from .config import Settings, save_location_policy
from .history import APPLIED, DISMISSED, History, RECOMMENDED
from .models import Posting

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = RunState()

    # --- data ------------------------------------------------------------
    def board_payload(self) -> Dict[str, Any]:
        settings = self.settings
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
        }

    # --- actions ---------------------------------------------------------
    def start_run(self, expand: bool = False) -> Dict[str, Any]:
        with self.state.lock:
            if self.state.running:
                return {"started": False, "reason": "a run is already in progress"}
            self.state.running = True
            self.state.log = []
            self.state.error = ""
            self.state.summary = ""
            self.state.finished_at = None

        def work() -> None:
            pipeline.set_logger(self.state.append)
            try:
                result = pipeline.find(self.settings, expand=expand)
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
        history = History(self.settings.history_path)
        entry = history.mark(posting_id, status)
        return {"ok": entry is not None}

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
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(settings: Settings, port: Optional[int] = None,
          open_browser: bool = True) -> int:
    settings.ensure_data_dir()
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
