"""Runtime configuration.

Every path that could hold personal information is a *setting*. Nothing about any
particular person is hardcoded anywhere in this package, and :func:`load_settings`
hard-fails if a personal path resolves inside the repository — the single most
likely way private material would ever reach a public git history.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

DEFAULT_DATA_DIR = Path.home() / ".jobscout"


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or unsafe."""


def _load_env_file(path: Path) -> None:
    """Minimal .env loader (same idea as covered-call-app, without the dependency).

    Real environment variables always win, so `JOBSCOUT_X=1 jobscout find` overrides
    the file for a single run.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError("%s must be an integer, got %r" % (name, raw)) from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = _env(name)
    if not raw:
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class LocationPolicy:
    """The HARD location filter.

    This is deliberately *not* an instruction to a model. The agents are told the
    policy so they search sensibly, but every candidate is re-checked in Python
    (:mod:`jobscout.filters`) before it can reach a report. A model that decides
    Denver is "close enough" gets overruled.
    """

    #: Postal codes of states/regions that count as onsite-acceptable, e.g. ["NM"].
    allowed_states: List[str] = field(default_factory=list)
    #: Extra city/metro names that count as onsite-acceptable, lowercased on load.
    allowed_cities: List[str] = field(default_factory=list)
    #: Does a fully-remote role qualify regardless of where the company sits?
    allow_remote: bool = True
    #: A remote role is still rejected if it is fenced to a region that is not ours,
    #: e.g. "Remote (must reside in California)". These are the regions we DO accept
    #: such a fence to name; empty means "only our allowed states/cities".
    remote_allowed_regions: List[str] = field(default_factory=list)
    #: Reject hybrid roles, which require living near the office.
    allow_hybrid: bool = False
    #: Human-readable summary handed to the search agents.
    description: str = ""

    def normalized(self) -> "LocationPolicy":
        return LocationPolicy(
            allowed_states=[s.strip().upper() for s in self.allowed_states if s.strip()],
            allowed_cities=[c.strip().lower() for c in self.allowed_cities if c.strip()],
            allow_remote=self.allow_remote,
            remote_allowed_regions=[r.strip().lower() for r in self.remote_allowed_regions if r.strip()],
            allow_hybrid=self.allow_hybrid,
            description=self.description,
        )

    def summary(self) -> str:
        if self.description:
            return self.description
        parts = []
        if self.allowed_states:
            parts.append("onsite in " + "/".join(self.allowed_states))
        if self.allow_remote:
            parts.append("or fully remote")
        return ", ".join(parts) or "unrestricted"


@dataclass
class Settings:
    #: Folder of your existing applications. One subfolder per company is the
    #: expected shape, but a flat folder of documents works too. NEVER in the repo.
    applications_dir: Path
    #: Where jobscout keeps its profile, history and reports. NEVER in the repo.
    data_dir: Path
    #: "cli" (your logged-in Claude Code account), "anthropic" (API key), or "mock".
    backend: str = "cli"
    model_cheap: str = "claude-haiku-4-5"
    model_strong: str = "claude-opus-5"
    #: A posting older than this many days is dropped as stale.
    max_age_days: int = 30
    #: How many search angles the discovery pass runs, and how many roles it reports.
    search_queries: int = 6
    max_results: int = 10
    #: Concurrency for the per-query / per-candidate agent calls.
    max_workers: int = 4
    #: Seconds before a single agent call is abandoned.
    timeout_seconds: int = 600
    location: LocationPolicy = field(default_factory=LocationPolicy)
    #: Titles/domains to steer the search; inferred from your materials if empty.
    target_titles: List[str] = field(default_factory=list)
    exclude_companies: List[str] = field(default_factory=list)

    # --- derived paths -----------------------------------------------------
    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.json"

    @property
    def history_path(self) -> Path:
        return self.data_dir / "history.jsonl"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


def _assert_outside_repo(path: Path, label: str) -> None:
    """Refuse to use a personal path that lives inside the git repo."""
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - unusual filesystem states
        raise ConfigError("could not resolve %s (%s): %s" % (label, path, exc)) from exc
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return  # outside the repo: good
    raise ConfigError(
        "%s points INSIDE the jobscout repo (%s).\n"
        "That folder holds personal material and this repo is public — put it "
        "somewhere else, e.g. under your home directory." % (label, resolved)
    )


def load_location_policy(data_dir: Path) -> LocationPolicy:
    """Location policy from ``<data_dir>/location.json``, overridden by env vars."""
    policy = LocationPolicy()
    path = data_dir / "location.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        policy = LocationPolicy(
            allowed_states=raw.get("allowed_states", []),
            allowed_cities=raw.get("allowed_cities", []),
            allow_remote=raw.get("allow_remote", True),
            remote_allowed_regions=raw.get("remote_allowed_regions", []),
            allow_hybrid=raw.get("allow_hybrid", False),
            description=raw.get("description", ""),
        )
    if _env("JOBSCOUT_ALLOWED_STATES"):
        policy.allowed_states = _env_list("JOBSCOUT_ALLOWED_STATES")
    if _env("JOBSCOUT_ALLOWED_CITIES"):
        policy.allowed_cities = _env_list("JOBSCOUT_ALLOWED_CITIES")
    if _env("JOBSCOUT_ALLOW_REMOTE"):
        policy.allow_remote = _env_bool("JOBSCOUT_ALLOW_REMOTE", True)
    if _env("JOBSCOUT_ALLOW_HYBRID"):
        policy.allow_hybrid = _env_bool("JOBSCOUT_ALLOW_HYBRID", False)
    if _env("JOBSCOUT_REMOTE_ALLOWED_REGIONS"):
        policy.remote_allowed_regions = _env_list("JOBSCOUT_REMOTE_ALLOWED_REGIONS")
    return policy.normalized()


def save_location_policy(data_dir: Path, policy: LocationPolicy) -> Path:
    path = data_dir / "location.json"
    path.write_text(json.dumps(asdict(policy), indent=2) + "\n", encoding="utf-8")
    return path


def load_settings(require_applications: bool = True) -> Settings:
    _load_env_file(ENV_FILE)

    apps_raw = _env("JOBSCOUT_APPLICATIONS_DIR")
    if not apps_raw and require_applications:
        raise ConfigError(
            "JOBSCOUT_APPLICATIONS_DIR is not set.\n"
            "Point it at the folder holding the applications you have already "
            "written (one subfolder per company works best), e.g.\n"
            '  JOBSCOUT_APPLICATIONS_DIR="$HOME/Desktop/Job Search"\n'
            "Run `jobscout init` to write a starter .env."
        )
    applications_dir = Path(os.path.expanduser(apps_raw)) if apps_raw else Path(os.devnull)

    data_raw = _env("JOBSCOUT_DATA_DIR")
    data_dir = Path(os.path.expanduser(data_raw)) if data_raw else DEFAULT_DATA_DIR

    if apps_raw:
        _assert_outside_repo(applications_dir, "JOBSCOUT_APPLICATIONS_DIR")
        if require_applications and not applications_dir.is_dir():
            raise ConfigError(
                "JOBSCOUT_APPLICATIONS_DIR does not exist: %s" % applications_dir)
    _assert_outside_repo(data_dir, "JOBSCOUT_DATA_DIR")

    backend = _env("JOBSCOUT_BACKEND", "cli").lower()
    if backend not in ("cli", "anthropic", "mock"):
        raise ConfigError("JOBSCOUT_BACKEND must be cli, anthropic or mock (got %r)" % backend)

    settings = Settings(
        applications_dir=applications_dir,
        data_dir=data_dir,
        backend=backend,
        model_cheap=_env("JOBSCOUT_MODEL_CHEAP", "claude-haiku-4-5"),
        model_strong=_env("JOBSCOUT_MODEL_STRONG", "claude-opus-5"),
        max_age_days=_env_int("JOBSCOUT_MAX_AGE_DAYS", 30),
        search_queries=_env_int("JOBSCOUT_SEARCH_QUERIES", 6),
        max_results=_env_int("JOBSCOUT_MAX_RESULTS", 10),
        max_workers=_env_int("JOBSCOUT_MAX_WORKERS", 4),
        timeout_seconds=_env_int("JOBSCOUT_TIMEOUT_SECONDS", 600),
        location=load_location_policy(data_dir),
        target_titles=_env_list("JOBSCOUT_TARGET_TITLES"),
        exclude_companies=_env_list("JOBSCOUT_EXCLUDE_COMPANIES"),
    )
    return settings


def redact(path: Path) -> str:
    """Render a path with the home directory collapsed, for logs and reports."""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home):
        return "~" + text[len(home):]
    return text
