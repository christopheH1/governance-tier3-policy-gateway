"""Ledger write measurement: N sequential writes, reporting rate, refusals, and write time.

Usage:  python ledger_burst.py [N]      (default 200)
Note:   this adds N events to the ledger (2N entries, since Vestigia also records each request).
Exit code is non-zero if any write was refused, for example by the rate limit.
"""
import os
import statistics
import sys
import time

import requests

VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")
HEADERS = {"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"}
EVENT = {"actor_id": "ledger-burst", "action_type": "MEASUREMENT", "status": "SUCCESS",
         "evidence": {"tool": "database_read", "params": {"limit": 1}, "purpose": "write-time measurement"}}

count = int(sys.argv[1]) if len(sys.argv) > 1 else 200
session = requests.Session()
before = session.get(f"{VESTIGIA}/health", timeout=10).json().get("total_events")
codes, times = {}, []
started = time.perf_counter()
for _ in range(count):
    t0 = time.perf_counter()
    status = session.post(f"{VESTIGIA}/events", json=EVENT, headers=HEADERS, timeout=30).status_code
    times.append((time.perf_counter() - t0) * 1000)
    codes[status] = codes.get(status, 0) + 1
elapsed = time.perf_counter() - started

slice_size = max(count // 10, 1)
print(f"ledger entries before: {before}")
print(f"{count} writes in {elapsed:.1f} s = {count / elapsed:.1f} a second, status codes {codes}")
print(f"write time: first {slice_size} median {statistics.median(times[:slice_size]):.1f} ms, "
      f"last {slice_size} median {statistics.median(times[-slice_size:]):.1f} ms, "
      f"p95 {sorted(times)[int(count * 0.95) - 1]:.1f} ms")
refused = count - codes.get(201, 0)
print("no write was refused" if refused == 0 else f"{refused} writes were refused")
sys.exit(1 if refused else 0)
