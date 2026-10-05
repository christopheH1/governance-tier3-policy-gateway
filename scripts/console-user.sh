#!/usr/bin/env bash
# Manages the people who may sign in to the console. Each has their own name, password, and roles.
#   ./scripts/console-user.sh add christophe                       all three roles
#   ./scripts/console-user.sh add mei --roles reviewer             a reviewer only
#   ./scripts/console-user.sh reset christophe                     a new password
#   ./scripts/console-user.sh remove mei
#   ./scripts/console-user.sh list
# A new password is printed once and stored nowhere.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T console python users.py "$@"
