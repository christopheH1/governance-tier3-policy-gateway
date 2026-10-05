#!/usr/bin/env bash
# Runs the latency harness and saves its results under results/.
#
#   ./scripts/latency-run.sh gateway                      per-stage latency through the policy gateway
#   ./scripts/latency-run.sh gateway --requests 500 --workers 1,2,4
#   ./scripts/latency-run.sh ledger-curve                 audit write time against ledger size (long: up to an hour)
#   ./scripts/latency-run.sh ledger-curve --target-entries 3000
#
# Each run saves three files: the full results (.json), the report as shown (.txt),
# and what was measured (.meta.txt: ARTO commit, image identifiers, host).
set -uo pipefail
cd "$(dirname "$0")/.."

mode="${1:-}"
case "$mode" in
  gateway|ledger-curve) shift ;;
  *) echo "usage: $0 gateway|ledger-curve [options]" >&2; exit 2 ;;
esac

mkdir -p results
stamp="$(date +%Y%m%d-%H%M%S)"
base="results/latency-${mode}-${stamp}"

{
  echo "run:          ${mode} $*"
  echo "started:      $(date -Is)"
  echo "host:         $(uname -srm)"
  echo "cpus:         $(nproc)"
  echo "memory:       $(awk '/MemTotal/ {printf "%.1f GB", $2/1048576}' /proc/meminfo)"
  echo "ARTO commit:  $(git -C vendor/ARTO rev-parse HEAD 2>/dev/null)"
  echo "docker:       $(docker version --format '{{.Server.Version}}' 2>/dev/null </dev/null)"
  echo "images:"
  docker compose images 2>/dev/null </dev/null | sed 's/^/  /'
} > "${base}.meta.txt"

docker compose --profile test run --rm -T e2e python latency_harness.py "$mode" "$@" \
  </dev/null > "${base}.json" 2> >(tee "${base}.txt" >&2)
status=$?
wait

echo
echo "saved: ${base}.json, ${base}.txt, ${base}.meta.txt"
exit "$status"
