#!/usr/bin/env bash
# Fetches ARTO into vendor/ARTO and pins it to the commit this build was verified against.
set -euo pipefail
cd "$(dirname "$0")/.."

ARTO_URL="https://github.com/ArksherX/ARTO.git"
ARTO_COMMIT="b57be5e787560d1e1531c7a30075c9cc681cea16"

mkdir -p vendor
if [ ! -d vendor/ARTO/.git ]; then
  git clone "$ARTO_URL" vendor/ARTO
fi
git -C vendor/ARTO fetch --quiet origin
git -C vendor/ARTO checkout --quiet --detach "$ARTO_COMMIT"

actual="$(git -C vendor/ARTO rev-parse HEAD)"
if [ "$actual" != "$ARTO_COMMIT" ]; then
  echo "ARTO is at $actual, expected $ARTO_COMMIT" >&2
  exit 1
fi
if [ -n "$(git -C vendor/ARTO status --porcelain)" ]; then
  echo "vendor/ARTO has local changes. The build must use the unmodified commit." >&2
  exit 1
fi
echo "ARTO pinned at $actual"
