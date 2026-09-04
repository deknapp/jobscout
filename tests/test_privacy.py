"""This repo is public. These tests are the reason that is safe.

Nothing about any particular person may live in the tree, and the tool must
refuse to keep personal material anywhere git could pick it up.
"""
import subprocess
from pathlib import Path

import pytest

from jobscout import config
from jobscout.config import ConfigError, REPO_ROOT, load_settings

TRACKED_TEXT_SUFFIXES = {".py", ".md", ".toml", ".txt", ".cfg", ".yml", ".yaml",
                         ".json", ".sh", ".example", ""}


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT),
                         capture_output=True, text=True)
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def test_the_applications_folder_may_not_live_in_the_repo(monkeypatch, tmp_path):
    inside = REPO_ROOT / "applications"
    monkeypatch.setenv("JOBSCOUT_APPLICATIONS_DIR", str(inside))
    monkeypatch.setenv("JOBSCOUT_DATA_DIR", str(tmp_path))
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "INSIDE the jobscout repo" in str(excinfo.value)


def test_the_data_dir_may_not_live_in_the_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSCOUT_APPLICATIONS_DIR", str(tmp_path))
    monkeypatch.setenv("JOBSCOUT_DATA_DIR", str(REPO_ROOT / "data"))
    with pytest.raises(ConfigError):
        load_settings()


def test_gitignore_covers_the_dangerous_shapes():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.pdf", "*.docx", "history.jsonl", "profile.json",
                    "reports/", "data/", "applications/"):
        assert pattern in text, "%s is not git-ignored" % pattern


def test_no_env_file_is_tracked():
    tracked = {p.name for p in _tracked_files()}
    assert ".env" not in tracked
    assert ".env.example" in tracked  # the template is fine, it has no values


def test_no_application_documents_are_tracked():
    bad = [p for p in _tracked_files()
           if p.suffix.lower() in (".pdf", ".docx", ".doc", ".rtf")]
    assert not bad, "application documents are tracked: %s" % bad


def test_the_source_tree_hardcodes_nobody():
    """No personal name, path or employer list is baked into the code.

    Everything person-specific arrives through configuration, so the same code
    serves anyone who clones it.
    """
    suspicious = ("/Users/", "/home/", "Desktop/Job Search")
    offenders = []
    for path in _tracked_files():
        if path.suffix.lower() not in TRACKED_TEXT_SUFFIXES or not path.exists():
            continue
        if path.name in (Path(__file__).name, "install-hooks.sh"):
            continue  # these two name the needles on purpose
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in suspicious:
            if needle in text:
                offenders.append("%s contains %r" % (path.name, needle))
    assert not offenders, offenders


def test_default_data_dir_is_outside_the_repo():
    assert REPO_ROOT not in config.DEFAULT_DATA_DIR.parents
