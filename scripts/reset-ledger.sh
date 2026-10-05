#!/usr/bin/env bash
# Deletes the audit ledger and starts a new, empty one. Lab use only.
# For measuring the ledger curve from an empty ledger. Agents, tokens, and policy are kept.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "This permanently deletes the audit ledger (volume governance-tier3_vestigia-data)."
read -r -p "Type 'delete ledger' to continue: " answer
[ "$answer" = "delete ledger" ] || { echo "nothing changed"; exit 1; }

docker compose rm --stop --force vestigia
docker volume rm governance-tier3_vestigia-data
docker compose up -d --wait vestigia
echo "new empty ledger started"
