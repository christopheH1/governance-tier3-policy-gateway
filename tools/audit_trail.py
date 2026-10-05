"""Shows the audit trail of one task, read straight from the ledger files.

It does not call Vestigia, the gateway, or the console. It reads the same files
the independent verifier reads, recomputes every hash, and prints the entries
that belong to one task in the order they were written.

Usage
  audit_trail.py                 list the recent tasks
  audit_trail.py --last          the trail of the most recent task
  audit_trail.py --task run-...  the trail of one task
  audit_trail.py --trace <id>    the trail of the task a trace identifier belongs to
  audit_trail.py --tamper-demo   alter a copy of the ledger and show the verifier catching it

Standard library only. VESTIGIA_SECRET_SALT must be set to recompute the hashes.
"""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verify_ledger import entry_hash, load, verify

LOCAL = timezone(timedelta(hours=int(os.environ.get("DISPLAY_UTC_OFFSET", "8"))))
PLAIN = {
    ("MODEL_DECISION", "ALLOWED"): "Gateway allowed a model call",
    ("MODEL_DECISION", "DENIED"): "Gateway DENIED a model call",
    ("MODEL_COMPLETED", "SUCCESS"): "Model answered",
    ("MODEL_COMPLETED", "ERROR"): "Model call failed",
    ("TOOL_DECISION", "ALLOWED"): "Gateway allowed a tool call",
    ("TOOL_DECISION", "DENIED"): "Gateway DENIED a tool call",
    ("TOOL_DECISION", "REFERRED_FLAG"): "Gateway allowed a tool call and FLAGGED it for review",
    ("TOOL_EXECUTED", "SUCCESS"): "Tool ran",
    ("HOLD_CREATED", "REFERRED_HOLD"): "Gateway HELD a tool call for a reviewer",
    ("HOLD_APPROVED", "APPROVED"): "Reviewer APPROVED the held call",
    ("HOLD_REJECTED", "DENIED"): "Reviewer REJECTED the held call",
    ("HOLD_RELEASED", "ALLOWED"): "Gateway released the approved call",
    ("HOLD_EXPIRED", "DENIED"): "Hold expired unanswered, so the call was DENIED",
    ("INTENT_CHECK", "FITS"): "Checker found the request fits the declared purpose",
    ("INTENT_CHECK", "MISMATCH"): "Checker FLAGGED the request: it may not fit the declared purpose",
    ("INTENT_CHECK", "UNCLEAR"): "Checker gave no clear verdict on whether the request fits the purpose",
    ("INTENT_CHECK", "NOT_CHECKED"): "The request was NOT checked against the declared purpose",
}


def all_entries(data_dir: Path) -> list:
    archives = sorted((data_dir / "archives").glob("ledger_*_entries.json"))
    entries = []
    for path in archives + [data_dir / "vestigia_ledger.json"]:
        for index, entry in enumerate(load(path)):
            entries.append((path.name, index, entry))
    return entries


def when(entry: dict) -> str:
    try:
        return datetime.fromisoformat(entry["timestamp"]).astimezone(LOCAL).strftime("%H:%M:%S.%f")[:-3]
    except (KeyError, ValueError):
        return "?"


def tasks(entries: list) -> dict:
    """task_id -> summary, in order of first appearance."""
    found = {}
    for _, _, entry in entries:
        intent = (entry.get("evidence") or {}).get("intent") or {}
        task = intent.get("task_id")
        if not task:
            continue
        item = found.setdefault(task, {"agent": entry.get("actor_id"), "started": entry, "purpose": intent.get("purpose"),
                                       "statement": intent.get("statement"), "decisions": 0})
        item["decisions"] += 1
    return found


def trail(entries: list, task: str) -> list:
    traces, holds = set(), set()
    for _, _, entry in entries:
        evidence = entry.get("evidence") or {}
        if (evidence.get("intent") or {}).get("task_id") == task:
            traces.add(evidence.get("trace_id"))
            if evidence.get("hold_reference"):
                holds.add(evidence["hold_reference"])
    traces.discard(None)
    selected = []
    for item in entries:
        evidence = item[2].get("evidence") or {}
        if not isinstance(evidence, dict):
            continue
        if evidence.get("trace_id") in traces or evidence.get("hold_reference") in holds \
                or evidence.get("released_from_hold") in holds:
            holds.update(filter(None, [evidence.get("hold_reference")]))
            selected.append(item)
    return selected


def details(entry: dict) -> list:
    evidence = entry.get("evidence") or {}
    lines = []
    def add(label, value):
        if value not in (None, "", [], {}):
            lines.append(f"{label}: {value if isinstance(value, str) else json.dumps(value)}")
    add("model", evidence.get("model"))
    add("tool", evidence.get("tool"))
    add("parameters", evidence.get("params"))
    add("reasons", ", ".join(evidence.get("reasons") or []))
    add("tools shown to the model", ", ".join(evidence.get("tools_offered") or []))
    add("tools removed by the gateway", ", ".join(evidence.get("tools_removed") or []))
    add("model asked to use", ", ".join(evidence.get("proposed_tool_calls") or []))
    add("tokens used", (evidence.get("usage") or {}).get("total_tokens"))
    add("on behalf of", (evidence.get("intent") or {}).get("on_behalf_of"))
    add("checked by", evidence.get("checked_by"))
    add("checker's reason", evidence.get("reason"))
    add("the request, as flagged", evidence.get("request_text"))
    add("reviewer", evidence.get("reviewer"))
    add("reviewer's note", evidence.get("note"))
    add("hold reference", evidence.get("hold_reference"))
    add("fingerprint of the request", evidence.get("request_sha256") or evidence.get("prompt_sha256"))
    add("fingerprint of the response", evidence.get("response_sha256"))
    add("credential used", evidence.get("token_jti"))
    add("trace", evidence.get("trace_id"))
    return lines


def show_trail(entries: list, task: str, salt: str) -> bool:
    selected = trail(entries, task)
    if not selected:
        print(f"no ledger entries for task {task}")
        return False
    summary = tasks(entries)[task]
    print(f"Audit trail of task {task}")
    print(f"  agent:    {summary['agent']}")
    print(f"  purpose:  {summary['purpose']}")
    people = sorted({(e.get("evidence") or {}).get("intent", {}).get("on_behalf_of") for _, _, e in selected
                     if isinstance((e.get("evidence") or {}).get("intent"), dict)} - {None})
    print(f"  asked by: {', '.join(people) if people else 'not recorded'}")
    print(f"  task:     {summary['statement']}")
    print()
    good = True
    actors = sorted({entry.get("actor_id") for _, _, entry in selected})
    several = len(actors) > 1
    if several:
        print(f"  agents in this task: {', '.join(actors)}\n")
    for number, (file, index, entry) in enumerate(selected, start=1):
        matches = entry_hash(entry, salt) == entry.get("integrity_hash")
        good = good and matches
        title = PLAIN.get((entry.get("action_type"), entry.get("status")),
                          f"{entry.get('action_type')} {entry.get('status')}")
        print(f"{number:>2}. {when(entry)}  {title}" + (f"  [{entry.get('actor_id')}]" if several else ""))
        for line in details(entry):
            print(f"      {line}")
        print(f"      ledger entry {index} ({entry.get('event_id')}), hash {str(entry.get('integrity_hash'))[:16]}..."
              f"  {'recomputed, matches' if matches else 'DOES NOT MATCH ITS CONTENTS'}")
        print()
    return good


def chain_verdict(data_dir: Path, salt: str) -> bool:
    result = verify(data_dir, salt)
    print(f"Whole ledger: {result['entries_total']} entries, {result['entries_hash_checked']} hashes recomputed, "
          f"{'HMAC-SHA256 with the ledger salt' if result['salted'] else 'NO SALT SET'}")
    print(f"Head hash:    {result['head_hash']}")
    for item in result["problems"][:5]:
        print(f"  PROBLEM {item['problem']} at entry {item['index']} ({item['event_id']}): {item['detail']}")
    if len(result["problems"]) > 5:
        print(f"  ... and {len(result['problems']) - 5} more")
    print("RESULT:       " + ("the chain verifies: no entry was changed, removed, or inserted" if result["valid"]
                              else f"NOT VALID, {len(result['problems'])} problem(s)"))
    return result["valid"]


def tamper_demo(data_dir: Path, salt: str) -> int:
    """Works on a copy. The real ledger is opened read-only and is never written."""
    work = Path(tempfile.mkdtemp(prefix="ledger-copy-"))
    try:
        print("The real ledger, untouched:")
        if not chain_verdict(data_dir, salt):
            print("the real ledger does not verify, so the demonstration stops here")
            return 1
        original = load(data_dir / "vestigia_ledger.json")
        targets = [i for i, e in enumerate(original) if e.get("action_type") in ("TOOL_DECISION", "HOLD_REJECTED",
                   "HOLD_CREATED", "MODEL_DECISION") and i < len(original) - 1]
        refused = [i for i in targets if original[i].get("status") in ("DENIED", "REFERRED_HOLD")]
        if not targets:
            print("\nthe ledger holds no decision to alter yet: run a task first")
            return 1
        target = (refused or targets)[-1]

        def attempt(title, change):
            copy_dir = work / title.split(":")[0].replace(" ", "-")
            (copy_dir / "archives").mkdir(parents=True)
            for archive in (data_dir / "archives").glob("ledger_*_entries.json"):
                shutil.copy(archive, copy_dir / "archives" / archive.name)
            altered = copy.deepcopy(original)
            change(altered)
            (copy_dir / "vestigia_ledger.json").write_text(json.dumps(altered))
            print(f"\n{title}")
            return chain_verdict(copy_dir, salt)

        entry = original[target]
        def rewrite(entries):
            entries[target]["status"] = "ALLOWED"
        def remove(entries):
            del entries[target]
        def forge(entries):   # an insider without the salt recomputes the hash as well as they can
            entries[target]["status"] = "ALLOWED"
            entries[target]["integrity_hash"] = entry_hash(entries[target], "")
        caught = [
            not attempt(f"Attempt 1: in a copy, entry {target} ({entry.get('action_type')} {entry.get('status')} for "
                        f"{entry.get('actor_id')}) is rewritten to ALLOWED", rewrite),
            not attempt(f"Attempt 2: in a copy, entry {target} is deleted", remove),
            not attempt(f"Attempt 3: in a copy, entry {target} is rewritten and its hash recomputed without the ledger salt",
                        forge),
        ]
        print(f"\n{sum(caught)} of 3 alterations were detected. The real ledger was not written to.")
        return 0 if all(caught) else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--task")
    parser.add_argument("--trace")
    parser.add_argument("--last", action="store_true")
    parser.add_argument("--tamper-demo", action="store_true")
    args = parser.parse_args()
    salt = os.environ.get("VESTIGIA_SECRET_SALT", "")
    data_dir = Path(args.data_dir)
    try:
        if args.tamper_demo:
            return tamper_demo(data_dir, salt)
        entries = all_entries(data_dir)
    except (OSError, ValueError) as exc:
        print(f"could not read the ledger: {exc}", file=sys.stderr)
        return 2

    known = tasks(entries)
    task = args.task
    if args.trace:
        for _, _, entry in entries:
            evidence = entry.get("evidence") or {}
            if isinstance(evidence, dict) and evidence.get("trace_id") == args.trace and evidence.get("intent"):
                task = evidence["intent"].get("task_id")
        if not task:
            print(f"no task found for trace {args.trace}")
            return 1
    if args.last and known:
        task = list(known)[-1]
    if not task:
        print(f"{len(known)} task(s) in the ledger. Most recent last.\n")
        for name, item in list(known.items())[-20:]:
            print(f"  {when(item['started'])}  {name:<18} {item['agent']:<20} {item['purpose'] or '-':<18} "
                  f"{(item['statement'] or '')[:60]}")
        print("\nShow one with --task <name>, or the most recent with --last.")
        return 0
    if task not in known:
        print(f"no ledger entries for task {task}")
        return 1
    good = show_trail(entries, task, salt)
    return 0 if (chain_verdict(data_dir, salt) and good) else 1


if __name__ == "__main__":
    sys.exit(main())
