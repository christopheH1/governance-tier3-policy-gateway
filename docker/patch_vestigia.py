"""Build-time patch for Vestigia: make the API rate limit configurable.

Upstream (ARTO commit b57be5e) fixes the limit in source at 10 requests a second
with a burst of 20 per client address. This replaces that one line so the values
come from the environment, with the upstream values as defaults. Nothing else changes.

The build fails if the line is not found exactly once, so an upstream change
cannot be patched silently or wrongly.
"""
import pathlib
import sys

TARGET = pathlib.Path(sys.argv[1])
OLD = "rate_limiter = TokenBucket(rate=10.0, capacity=20.0)"
NEW = ('rate_limiter = TokenBucket(rate=float(os.getenv("VESTIGIA_RATE_LIMIT_RPS", "10")), '
       'capacity=float(os.getenv("VESTIGIA_RATE_LIMIT_BURST", "20")))')

source = TARGET.read_text()
if source.count(OLD) != 1:
    sys.exit(f"patch_vestigia: expected exactly one occurrence of the rate limit line, found {source.count(OLD)}")
if "\nimport os\n" not in source:
    sys.exit("patch_vestigia: api_server.py no longer imports os at module level")
TARGET.write_text(source.replace(OLD, NEW))
print("patch_vestigia: rate limit is now set by VESTIGIA_RATE_LIMIT_RPS and VESTIGIA_RATE_LIMIT_BURST")
