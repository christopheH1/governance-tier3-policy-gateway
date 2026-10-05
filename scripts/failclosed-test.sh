#!/usr/bin/env bash
# Fail-closed test: stop each dependency in turn and show that the policy gateway
# denies the call and the tool is not reached, then that service resumes when the
# dependency returns. Run with the stack up:  ./scripts/failclosed-test.sh
set -uo pipefail
cd "$(dirname "$0")/.."

# --no-deps matters: without it, compose would restart the dependency under test.
# </dev/null matters too: docker compose run must not read this script's input.
probe() { docker compose --profile test run --rm -T --no-deps e2e python failclosed_probe.py "$@" </dev/null; }

SERVICES=(opa tessera vestigia valkey)
LABELS=("policy engine (OPA)" "identity service (Tessera)" "audit ledger (Vestigia)" "key-value store (Valkey)")

passed=0
expected=$(( 1 + 2 * ${#SERVICES[@]} ))

probe setup || { echo "baseline failed, stopping"; exit 1; }
passed=$(( passed + 1 ))

for i in "${!SERVICES[@]}"; do
  service="${SERVICES[$i]}"
  label="${LABELS[$i]}"
  docker compose stop "$service" >/dev/null 2>&1 </dev/null
  probe expect-deny "$label" && passed=$(( passed + 1 ))
  docker compose up -d --wait "$service" >/dev/null 2>&1 </dev/null
  sleep 3
  probe expect-allow "$label" && passed=$(( passed + 1 ))
done

echo
echo "$passed of $expected checks passed"
if [ "$passed" -eq "$expected" ]; then
  echo "fail-closed test passed"
else
  echo "fail-closed test FAILED"
  exit 1
fi
