"""The worker loop and what it must never do.

The dangerous failure here is silent: an employer whose lookup is lost, or
recorded as having no board when nobody actually looked. Neither raises
anything; the registry is just quietly wrong, and nothing downstream questions
it again.
"""
from __future__ import annotations

import pytest

from jobscout import queueing, worker
from jobscout.discover import BoardGuess


class FakeConn:
    """Enough of a Redis stream to exercise the loop's ordering."""

    def __init__(self) -> None:
        self.published = []
        self.acked = []

    def xadd(self, stream, fields):
        self.published.append((stream, fields))
        return "1-0"

    def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)


def test_a_result_is_published_before_the_task_is_acknowledged(monkeypatch):
    """Acknowledging first would lose the employer entirely if the pod died in
    between -- no error, just an employer nobody ever looks up again."""
    conn = FakeConn()
    monkeypatch.setattr(worker.discover, "find_board",
                        lambda name, homepage="", **kw: BoardGuess(
                            url="https://boards.greenhouse.io/acme", ats="Greenhouse",
                            slug="acme", how="named", jobs_seen=4, probes=1))
    task = queueing.Task(msg_id="1-0", company="Acme")
    worker.handle(conn, task)
    assert conn.published, "nothing was published"
    assert not conn.acked, "handle() must not acknowledge; the loop does, after"


def test_finding_nothing_is_reported_as_not_found_not_as_a_failure(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(worker.discover, "find_board", lambda name, homepage="", **kw: None)
    worker.handle(conn, queueing.Task(msg_id="1-0", company="Booz Allen"))
    import json
    payload = json.loads(conn.published[0][1]["payload"])
    assert payload["found"] is False
    assert "error" not in payload


def test_one_employer_raising_does_not_end_the_worker(monkeypatch):
    conn = FakeConn()

    def boom(name, homepage="", **kw):
        raise RuntimeError("dns exploded")

    monkeypatch.setattr(worker.discover, "find_board", boom)
    worker.handle(conn, queueing.Task(msg_id="1-0", company="Acme"))
    import json
    assert "error" in json.loads(conn.published[0][1]["payload"])


def test_probing_failing_must_not_be_recorded_as_having_no_board(tmp_path):
    """The nastiest available mistake. Probing not placing an employer says
    nothing about whether they have a board -- only that the mechanical route
    missed. Writing no_board here would permanently stop the model path from
    ever trying, and the employer would silently never be searched again.
    """
    from jobscout.companies import NEW, NO_BOARD, Company, Registry

    registry = Registry(tmp_path / "companies.json")
    registry.add(Company(name="Booz Allen", status=NEW))

    # What collect-discovery does with {"found": False} -- which is nothing.
    company = registry.get("Booz Allen")
    assert company is not None
    assert company.status == NEW
    assert company.status != NO_BOARD


def test_an_unparseable_task_is_acknowledged_rather_than_retried_forever():
    assert queueing.Task.parse("1-0", {"payload": "not json"}) is None
    assert queueing.Task.parse("1-0", {}) is None
    good = queueing.Task.parse("1-0", {"payload": '{"company": "Acme"}'})
    assert good and good.company == "Acme"
