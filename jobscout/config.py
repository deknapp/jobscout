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


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError("%s must be a number, got %r" % (name, raw)) from exc


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
    #: e.g. "Remote (must reside in <some other state>)". These are the regions we accept
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
    #: How many roles a report shows.
    max_results: int = 10
    #: How many employers the registry should hold before it stops proposing
    #: more. The registry IS the search surface — thirty employers is a narrow
    #: search, and boards with an API cost nothing to read, so this is generous.
    company_target: int = 120
    #: How many employers to propose in one go when the registry is short.
    propose_batch: int = 25
    #: Per-run work caps — every one of these is a billed model call, so they are
    #: the dial between "cheap run" and "thorough run".
    max_resolve_per_run: int = 20
    #: Applies ONLY to boards with no API. Boards with one are read every run.
    max_scans_per_run: int = 8
    max_verify_per_run: int = 20
    #: Do not re-read an employer's board more often than this.
    rescan_after_days: int = 3
    #: A single employer's board can hold thousands of roles. Above this many
    #: (after the free location filter), the board is narrowed by title overlap
    #: before anything expensive happens.
    max_postings_per_company: int = 25
    #: Concurrency for the per-query / per-candidate agent calls.
    max_workers: int = 4
    #: Seconds before a single agent call is abandoned.
    timeout_seconds: int = 600
    location: LocationPolicy = field(default_factory=LocationPolicy)
    #: Salary, employment type, clearance and title filters — each with its own
    #: policy for postings that simply do not say.
    requirements: "Requirements" = field(default_factory=lambda: _requirements())
    #: How the three scores are blended, and how fast recency decays.
    weight_fit: float = 0.45
    weight_likelihood: float = 0.30
    weight_recency: float = 0.25
    recency_halflife_days: float = 14.0
    #: Port for `jobscout serve`.
    port: int = 8765
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

    @property
    def companies_path(self) -> Path:
        return self.data_dir / "companies.json"

    @property
    def board_path(self) -> Path:
        return self.data_dir / "board.json"

    @property
    def requirements_path(self) -> Path:
        return self.data_dir / "requirements.json"

    def weights(self):
        from .scoring import Weights

        return Weights(fit=self.weight_fit, likelihood=self.weight_likelihood,
                       recency=self.weight_recency,
                       halflife_days=self.recency_halflife_days)

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


def _requirements():
    from .requirements import Requirements

    return Requirements()


def _env_optional_int(name: str) -> Optional[int]:
    raw = _env(name)
    if not raw or raw.lower() in ("none", "any", "-"):
        return None
    try:
        return int(float(raw.lower().replace("k", "000").replace("$", "").replace(",", "")))
    except ValueError as exc:
        raise ConfigError("%s must be a number, got %r" % (name, raw)) from exc


def load_requirements(data_dir: Path):
    """Requirements from ``<data_dir>/requirements.json``, overridden by env vars."""
    from .requirements import EXCLUDE, INCLUDE, Requirements

    requirements = Requirements()
    path = data_dir / "requirements.json"
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
        fields = set(Requirements.__dataclass_fields__)  # type: ignore[attr-defined]
        requirements = Requirements(**{k: v for k, v in raw.items() if k in fields})

    if _env("JOBSCOUT_SALARY_MIN"):
        requirements.salary_min = _env_optional_int("JOBSCOUT_SALARY_MIN")
    if _env("JOBSCOUT_SALARY_MAX"):
        requirements.salary_max = _env_optional_int("JOBSCOUT_SALARY_MAX")
    for env_name, attr in (
            ("JOBSCOUT_UNKNOWN_SALARY", "unknown_salary"),
            ("JOBSCOUT_UNKNOWN_EMPLOYMENT", "unknown_employment"),
            ("JOBSCOUT_UNKNOWN_CLEARANCE", "unknown_clearance"),
            ("JOBSCOUT_UNKNOWN_LOCATION", "unknown_location"),
            ("JOBSCOUT_UNKNOWN_DATE", "unknown_date")):
        if _env(env_name):
            value = _env(env_name).lower()
            if value not in (INCLUDE, EXCLUDE):
                raise ConfigError("%s must be 'include' or 'exclude', got %r"
                                  % (env_name, value))
            setattr(requirements, attr, value)
    if _env("JOBSCOUT_EMPLOYMENT_TYPES"):
        requirements.employment_types = _env_list("JOBSCOUT_EMPLOYMENT_TYPES")
    if _env("JOBSCOUT_EXCLUDE_CLEARANCE_ROLES"):
        requirements.exclude_clearance_required = _env_bool(
            "JOBSCOUT_EXCLUDE_CLEARANCE_ROLES", False)
    if _env("JOBSCOUT_EXCLUDE_TITLE_WORDS"):
        requirements.exclude_title_words = _env_list("JOBSCOUT_EXCLUDE_TITLE_WORDS")
    if _env("JOBSCOUT_REQUIRE_TITLE_WORDS"):
        requirements.require_title_words = _env_list("JOBSCOUT_REQUIRE_TITLE_WORDS")
    return requirements.normalized()


def save_requirements(data_dir: Path, requirements) -> Path:
    path = data_dir / "requirements.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(requirements.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


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
            '  JOBSCOUT_APPLICATIONS_DIR="$HOME/job-applications"\n'
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

    requirements = load_requirements(data_dir)
    settings = Settings(
        applications_dir=applications_dir,
        data_dir=data_dir,
        backend=backend,
        model_cheap=_env("JOBSCOUT_MODEL_CHEAP", "claude-haiku-4-5"),
        model_strong=_env("JOBSCOUT_MODEL_STRONG", "claude-opus-5"),
        max_age_days=_env_int("JOBSCOUT_MAX_AGE_DAYS", 30),
        max_results=_env_int("JOBSCOUT_MAX_RESULTS", 10),
        company_target=_env_int("JOBSCOUT_COMPANY_TARGET", 120),
        propose_batch=_env_int("JOBSCOUT_PROPOSE_BATCH", 25),
        max_resolve_per_run=_env_int("JOBSCOUT_MAX_RESOLVE_PER_RUN", 20),
        max_scans_per_run=_env_int("JOBSCOUT_MAX_SCANS_PER_RUN", 8),
        max_verify_per_run=_env_int("JOBSCOUT_MAX_VERIFY_PER_RUN", 20),
        rescan_after_days=_env_int("JOBSCOUT_RESCAN_AFTER_DAYS", 3),
        max_postings_per_company=_env_int("JOBSCOUT_MAX_POSTINGS_PER_COMPANY", 25),
        weight_fit=_env_float("JOBSCOUT_WEIGHT_FIT", 0.45),
        weight_likelihood=_env_float("JOBSCOUT_WEIGHT_LIKELIHOOD", 0.30),
        weight_recency=_env_float("JOBSCOUT_WEIGHT_RECENCY", 0.25),
        recency_halflife_days=_env_float("JOBSCOUT_RECENCY_HALFLIFE_DAYS", 14.0),
        port=_env_int("JOBSCOUT_PORT", 8765),
        max_workers=_env_int("JOBSCOUT_MAX_WORKERS", 4),
        timeout_seconds=_env_int("JOBSCOUT_TIMEOUT_SECONDS", 600),
        location=load_location_policy(data_dir),
        requirements=requirements,
        target_titles=_env_list("JOBSCOUT_TARGET_TITLES"),
        exclude_companies=_env_list("JOBSCOUT_EXCLUDE_COMPANIES"),
    )
    # A saved freshness limit wins over the built-in default, but never over an
    # explicit environment variable.
    if requirements.max_age_days and not _env("JOBSCOUT_MAX_AGE_DAYS"):
        settings.max_age_days = requirements.max_age_days
    return settings


def redact(path: Path) -> str:
    """Render a path with the home directory collapsed, for logs and reports."""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home):
        return "~" + text[len(home):]
    return text
