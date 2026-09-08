#!/usr/bin/env bash
# The picture this repo is about: work waiting, against pods doing it.
set -uo pipefail
NS="${1:-jobscout}"
bar() { local n=$1 c=${2:-#} s=""; for ((i=0;i<n;i++)); do s+="$c"; done; printf '%-8s' "$s"; }

printf '%8s  %-10s %-10s  %s\n' TIME WORKERS QUEUE STATE
while true; do
  running=$(kubectl -n "$NS" get pods -l app.kubernetes.io/component=worker \
            --no-headers 2>/dev/null | grep -c Running)
  pending=$(kubectl -n "$NS" get pods -l app.kubernetes.io/component=worker \
            --no-headers 2>/dev/null | grep -c Pending)
  depth=$(kubectl -n "$NS" exec statefulset/redis -- redis-cli \
          --no-raw XINFO GROUPS jobscout:discovery 2>/dev/null \
          | tr -d '"' | awk '/lag/{getline; lag=$1} /pending/{getline; pend=$1} END{print (lag+0)+(pend+0)}')
  printf '%8s  %s   %s  running=%s pending=%s\n' \
    "$(date +%H:%M:%S)" "$(bar "${running:-0}")" "$(bar "${depth:-0}" =)" \
    "${running:-0}" "${pending:-0}"
  sleep 3
done
