"""A worker that looks up careers boards, and expects to be killed.

The loop is small on purpose: take one task, find the board, publish what it
found, acknowledge. Everything interesting is in what happens when that is
interrupted.

  * The task is acknowledged only after the result is published. A worker that
    dies in between leaves the task pending, and another worker reclaims it.
    Doing this the other way round loses employers silently, which is the
    worst failure this system can have -- nothing errors, the registry is just
    quietly short.
  * On SIGTERM it stops taking new work and finishes the task in hand. A probe
    is bounded by the rate limiter's deadline, so "finish the task in hand" is
    a few seconds, not an open-ended promise. There is nothing to gain by
    abandoning it.
  * Readiness reports on Redis. Liveness does not: Redis being down is not a
    reason to restart this pod, and wiring it that way turns one outage into a
    pool-wide crash loop.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import discover, queueing, ratelimit

log = logging.getLogger("jobscout.worker")

HEALTH_PORT = int(os.environ.get("JOBSCOUT_HEALTH_PORT", "9000"))

_stopping = threading.Event()
_stats = {"found": 0, "not_found": 0, "failed": 0, "started": time.time()}


def _handle_signal(signum, _frame) -> None:
    log.info("received %s; finishing the task in hand and stopping",
             signal.Signals(signum).name)
    _stopping.set()


def _serve_health(conn) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/healthz":
                self._send(200, b'{"ok":true}')
            elif path == "/readyz":
                try:
                    ready = bool(conn.ping()) and not _stopping.is_set()
                except Exception:  # noqa: BLE001
                    ready = False
                self._send(200 if ready else 503,
                           json.dumps({"ready": ready}).encode())
            elif path == "/stats":
                lag, pending = queueing.depth(conn)
                self._send(200, json.dumps({
                    **_stats, "uptime_s": round(time.time() - _stats["started"]),
                    "queue_lag": lag, "queue_pending": pending}).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def log_message(self, *args) -> None:
            # kubelet probes every few seconds; logging each one buries
            # everything the worker has to say.
            pass

    server = ThreadingHTTPServer(("0.0.0.0", HEALTH_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health").start()


def handle(conn, task: queueing.Task) -> None:
    try:
        guess = discover.find_board(task.company, task.homepage)
    except Exception as exc:  # noqa: BLE001 - one bad employer must not end the pod
        log.exception("looking up %s failed", task.company)
        queueing.publish(conn, task.company, {"error": str(exc)[:200]})
        _stats["failed"] += 1
        return

    if guess is None:
        # Not a failure. It means "probing could not place this one", and the
        # registry records that so a model call can be spent on it later.
        queueing.publish(conn, task.company, {"found": False})
        _stats["not_found"] += 1
        log.info("%s: no board found by probing", task.company)
        return

    queueing.publish(conn, task.company, {
        "found": True, "url": guess.url, "ats": guess.ats,
        "how": guess.how, "jobs_seen": guess.jobs_seen, "probes": guess.probes})
    _stats["found"] += 1
    log.info("%s: %s %s (%s, %d probe(s))",
             task.company, guess.ats, guess.url, guess.how, guess.probes)


def main(redis_url: Optional[str] = None) -> int:
    logging.basicConfig(level=os.environ.get("JOBSCOUT_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn = queueing.client(redis_url)
    queueing.ensure_group(conn)

    # The whole reason this is worth running as a pool: without a shared
    # limiter, ten pods each politely limiting themselves still means ten
    # times the agreed rate arriving at Greenhouse.
    url = redis_url or os.environ.get("JOBSCOUT_REDIS_URL", "redis://localhost:6379/0")
    discover.set_limiter(ratelimit.RedisLimiter.from_url(url))

    _serve_health(conn)
    consumer = queueing.consumer_name()
    log.info("worker %s up", consumer)

    while not _stopping.is_set():
        try:
            tasks = queueing.take(conn, consumer, block_ms=5000)
        except Exception:  # noqa: BLE001 - a Redis restart must not end the pod
            log.exception("could not read the queue; retrying")
            time.sleep(2)
            continue
        for task in tasks:
            handle(conn, task)
            # Only now: the result is published, so losing this pod costs
            # nothing but a repeat.
            queueing.ack(conn, task.msg_id)

    log.info("worker %s stopped after %d found, %d not found, %d failed",
             consumer, _stats["found"], _stats["not_found"], _stats["failed"])
    return 0
