#!/usr/bin/env bash
# Seed a few employers, queue them, and assert the pool actually did the work.
#
# Deliberately checks behaviour and not results: whether a given employer has a
# Greenhouse board is someone else's decision and would make this test fail on
# their schedule. What is being tested is that queued work reaches a worker
# that did not exist when it was queued.
set -euo pipefail
NS=jobscout

echo "==> seeding a small registry"
kubectl -n "$NS" exec deploy/web -- python -c '
import json
names = ["Kairos Power", "Vannevar Labs", "PostEra", "Rescale", "Benchling",
         "Enthought", "Uncountable", "Second Front Systems"]
json.dump({"updated": "2026-01-01",
           "companies": [{"name": n, "status": "new"} for n in names]},
          open("/data/companies.json", "w"))
print("seeded", len(names), "employers")
'

echo "==> queueing"
kubectl -n "$NS" create job --from=cronjob/queue-discovery ci-queue >/dev/null
kubectl -n "$NS" wait --for=condition=complete job/ci-queue --timeout=120s >/dev/null
kubectl -n "$NS" logs job/ci-queue

echo "==> waiting for KEDA to start a worker that did not exist a moment ago"
scaled=0
for _ in $(seq 1 40); do
  n=$(kubectl -n "$NS" get pods -l app.kubernetes.io/component=worker \
      --no-headers 2>/dev/null | wc -l | tr -d ' ')
  if [ "$n" -gt 0 ]; then scaled=1; echo "  pool scaled to $n"; break; fi
  sleep 5
done
[ "$scaled" = "1" ] || { echo "the pool never left zero"; exit 1; }

echo "==> waiting for the results to arrive"
for _ in $(seq 1 40); do
  results=$(kubectl -n "$NS" exec statefulset/redis -- \
            redis-cli XLEN jobscout:discovery:results 2>/dev/null | tr -d '\r')
  echo "  results: ${results:-0}"
  [ "${results:-0}" -ge 8 ] && break
  sleep 5
done
[ "${results:-0}" -ge 8 ] || { echo "workers did not report on every employer"; exit 1; }

echo "==> collecting"
kubectl -n "$NS" create job --from=cronjob/collect-discovery ci-collect >/dev/null
kubectl -n "$NS" wait --for=condition=complete job/ci-collect --timeout=120s >/dev/null
kubectl -n "$NS" logs job/ci-collect

echo "==> the queue must be drained"
lag=$(kubectl -n "$NS" exec statefulset/redis -- redis-cli XINFO GROUPS jobscout:discovery 2>/dev/null | tr -d '\r' | grep -A1 -w lag | tail -1)
echo "  consumer group lag: ${lag:-unknown}"
echo "deploy check OK"
