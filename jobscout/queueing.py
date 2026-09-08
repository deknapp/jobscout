"""The work queue, for when discovery runs somewhere other than the CLI.

One task: "find the careers board for this employer". It is the right unit to
distribute first because it is the only stage that is pure I/O, needs no
model, holds no state, and is bounded by other people's servers rather than by
anything we control -- which is exactly the shape that benefits from more
workers and a shared rate limiter, and exactly the shape that does not benefit
from a bigger machine.

Redis Streams rather than a list, because a consumer group remembers which
messages were handed out and not acknowledged. That is precisely the state a
worker killed mid-task leaves behind, and the whole point of running these on
something that will kill them.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

STREAM = "jobscout:discovery"
RESULTS = "jobscout:discovery:results"
GROUP = "workers"
#: How long a delivered-but-unacknowledged task waits before another worker
#: may claim it. Short, because the usual cause is a pod being evicted rather
#: than a slow probe -- probing has its own deadline well under this.
RECLAIM_IDLE_MS = 60_000


def client(url: Optional[str] = None):
    import redis

    return redis.Redis.from_url(url or os.environ.get(
        "JOBSCOUT_REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


def consumer_name() -> str:
    return "%s-%d" % (socket.gethostname(), os.getpid())


def ensure_group(conn) -> None:
    try:
        conn.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:  # noqa: BLE001 - only BUSYGROUP is expected
        if "BUSYGROUP" not in str(exc):
            raise


@dataclass
class Task:
    msg_id: str
    company: str
    homepage: str = ""

    @classmethod
    def parse(cls, msg_id: str, fields: Dict[str, str]) -> Optional["Task"]:
        try:
            payload = json.loads(fields["payload"])
            return cls(msg_id=msg_id, company=payload["company"],
                       homepage=payload.get("homepage", ""))
        except (KeyError, json.JSONDecodeError):
            return None


def submit(conn, companies: List[Dict[str, str]]) -> int:
    """Enqueue employers to look up. Returns how many were added."""
    pipe = conn.pipeline(transaction=False)
    for entry in companies:
        pipe.xadd(STREAM, {"payload": json.dumps(entry)})
    return len(pipe.execute())


def take(conn, consumer: str, block_ms: int = 5000) -> List[Task]:
    """Claim abandoned work first, then new work.

    Abandoned first because it is older than anything undelivered, and because
    a task that has already been handed out once is the one at risk of being
    forgotten entirely.
    """
    _, reclaimed, _ = conn.xautoclaim(STREAM, GROUP, consumer,
                                      min_idle_time=RECLAIM_IDLE_MS, count=1)
    entries = reclaimed or []
    if not entries:
        response = conn.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1,
                                   block=block_ms)
        entries = response[0][1] if response else []

    tasks = []
    for msg_id, fields in entries:
        task = Task.parse(msg_id, fields)
        if task is None:
            # Unparseable, and retrying cannot make it parseable.
            ack(conn, msg_id)
            continue
        tasks.append(task)
    return tasks


def ack(conn, msg_id: str) -> None:
    conn.xack(STREAM, GROUP, msg_id)


def publish(conn, company: str, result: Dict[str, Any]) -> None:
    """Record what was found, for the collector to merge into the registry."""
    conn.xadd(RESULTS, {"payload": json.dumps({"company": company, **result,
                                               "at": time.time()})})


def drain_results(conn, count: int = 500) -> List[Dict[str, Any]]:
    """Read and remove everything the workers have reported."""
    entries = conn.xrange(RESULTS, count=count)
    out = []
    for msg_id, fields in entries:
        try:
            out.append(json.loads(fields["payload"]))
        except (KeyError, json.JSONDecodeError):
            pass
        conn.xdel(RESULTS, msg_id)
    return out


def depth(conn) -> Tuple[int, int]:
    """(undelivered, delivered-but-unfinished).

    Both matter to an autoscaler and they mean different things: the first is
    work nobody has started, the second is work a pod was holding when it
    died. Scaling on the first alone would shrink the pool while tasks are
    still outstanding.
    """
    try:
        groups = conn.xinfo_groups(STREAM)
    except Exception:  # noqa: BLE001 - stream may not exist yet
        return 0, 0
    for group in groups:
        if group.get("name") == GROUP:
            return int(group.get("lag") or 0), int(group.get("pending") or 0)
    return 0, 0
