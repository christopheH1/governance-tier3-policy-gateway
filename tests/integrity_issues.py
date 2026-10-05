"""Asks Vestigia why its own integrity check objects, and compares with the ledger files' timestamps.
Run in the e2e test container:  docker compose --profile test run --rm e2e python integrity_issues.py
"""
import collections
import os

import requests

VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")
report = requests.get(f"{VESTIGIA}/integrity", headers={"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"},
                      timeout=120).json()
print(f"Vestigia says: is_valid={report.get('is_valid')}, {report.get('total_entries')} entries")
issues = report.get("issues", [])
kinds = collections.Counter((i.get("severity"), i.get("type")) for i in issues)
for (severity, kind), count in sorted(kinds.items(), key=lambda item: -item[1]):
    print(f"  {count:>4} x {severity} {kind}")
print()
for issue in [i for i in issues if i.get("severity") in ("CRITICAL", "INVALID")][:8]:
    index = issue.get("entry_index")
    print(f"{issue.get('severity')} {issue.get('type')} at entry {index}: {issue.get('description')} {issue.get('evidence') or ''}")
    if isinstance(index, int) and index > 0:
        for offset in (index - 1, index):
            event = requests.get(f"{VESTIGIA}/events/event_{offset:06d}", timeout=30,
                                 headers={"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"}).json()
            print(f"    entry {offset}: {event.get('timestamp')}  {event.get('actor_id')}  {event.get('action_type')}")
