"""Independent verifier for the Vestigia audit ledger.

It reads the ledger files directly and recomputes every hash from the entry
contents. It does not call Vestigia and shares no code with it, so its verdict
does not depend on the system being audited.

Unlike Vestigia's built-in check, it follows the chain across rotation: the
archived ledgers in data/archives/ followed by the current ledger.

What it checks
  1. The first ledger begins with the genesis entry.
  2. Every later ledger begins with a rotation entry that carries the last hash
     of the ledger before it.
  3. Every entry's previous_hash equals the hash of the entry before it.
  4. Every entry's hash, recomputed from its contents with the ledger salt,
     equals the hash stored with it.
It also reports, separately, where a timestamp is earlier than the one before it.
That is a clock fault, not an alteration: the hash chain fixes content and order,
and takes the time from the host clock as given.

Usage
  VESTIGIA_SECRET_SALT=... python verify_ledger.py --data-dir /data
Exit code 0 if the whole chain verifies, 1 if not, 2 if it could not be read.

Standard library only.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path


def entry_hash(entry: dict, salt: str) -> str:
    """The hash Vestigia stores: over timestamp, tenant, actor, action, status,
    canonical evidence, and the previous hash. HMAC-SHA256 when a salt is set."""
    evidence = json.dumps(entry.get("evidence"), sort_keys=True, separators=(",", ":"))
    tenant = entry.get("tenant_id") or ""
    payload = (f"{entry.get('timestamp')}{tenant}{entry.get('actor_id')}{entry.get('action_type')}"
               f"{entry.get('status')}{evidence}{entry.get('previous_hash')}")
    if salt:
        return hmac.new(salt.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hashlib.sha256(payload.encode()).hexdigest()


def load(path: Path) -> list:
    with open(path) as handle:
        entries = json.load(handle)
    if not isinstance(entries, list):
        raise ValueError(f"{path.name} is not a list of entries")
    return entries


def verify(data_dir: Path, salt: str, ledger_name: str = "vestigia_ledger.json") -> dict:
    archives = sorted((data_dir / "archives").glob("ledger_*_entries.json"))
    segments = [(path, load(path)) for path in archives] + [(data_dir / ledger_name, load(data_dir / ledger_name))]
    problems, checked, anchors, last_hash = [], 0, 0, None
    clock_steps, last_time = [], None

    def problem(path, index, entry, kind, detail):
        problems.append({"file": path.name, "index": index, "event_id": (entry or {}).get("event_id"),
                         "problem": kind, "detail": detail})

    for number, (path, entries) in enumerate(segments):
        if not entries:
            problem(path, None, None, "EMPTY_LEDGER", "the file holds no entries")
            continue
        first = entries[0]
        anchors += 1
        if number == 0 and first.get("action_type") == "LEDGER_INITIALIZED":
            if first.get("previous_hash") != "GENESIS" or first.get("integrity_hash") != "ROOT":
                problem(path, 0, first, "BAD_GENESIS", "genesis entry does not carry the expected markers")
        elif first.get("action_type") == "LEDGER_ROTATED":
            if number == 0:
                problem(path, 0, first, "MISSING_ARCHIVE",
                        "the oldest ledger present begins with a rotation entry, so an earlier archive is missing")
            elif first.get("previous_hash") != last_hash:
                problem(path, 0, first, "BROKEN_ROTATION_LINK",
                        "rotation entry does not carry the last hash of the ledger before it")
            if first.get("integrity_hash") != first.get("previous_hash"):
                problem(path, 0, first, "BAD_ROTATION_ENTRY", "rotation entry hash and previous hash differ")
        else:
            problem(path, 0, first, "BAD_FIRST_ENTRY", f"unexpected first entry of type {first.get('action_type')}")

        previous = first.get("integrity_hash")
        for index, entry in enumerate(entries):
            stamp = str(entry.get("timestamp") or "")
            if last_time and stamp and stamp < last_time[2]:
                clock_steps.append({"file": path.name, "index": index, "event_id": entry.get("event_id"),
                                    "before": last_time[2], "at": stamp})
            if stamp:
                last_time = (path.name, index, stamp)
        for index, entry in enumerate(entries[1:], start=1):
            checked += 1
            if entry.get("previous_hash") != previous:
                problem(path, index, entry, "BROKEN_CHAIN", "previous_hash does not match the entry before")
            if entry_hash(entry, salt) != entry.get("integrity_hash"):
                problem(path, index, entry, "HASH_MISMATCH", "contents do not match the stored hash")
            previous = entry.get("integrity_hash")
        last_hash = previous

    return {"valid": not problems, "ledger_files": [path.name for path, _ in segments],
            "entries_total": sum(len(entries) for _, entries in segments),
            "entries_hash_checked": checked, "anchor_entries": anchors,
            "rotations": len(segments) - 1, "head_hash": last_hash, "salted": bool(salt), "clock_steps": clock_steps,
            "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="/data", help="Vestigia data directory")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()
    salt = os.environ.get("VESTIGIA_SECRET_SALT", "")
    try:
        result = verify(Path(args.data_dir), salt)
    except (OSError, ValueError) as exc:
        print(f"could not read the ledger: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"ledger files:  {len(result['ledger_files'])} ({result['rotations']} rotation(s))")
        print(f"entries:       {result['entries_total']} in total, {result['entries_hash_checked']} hashes recomputed, "
              f"{result['anchor_entries']} genesis or rotation entries")
        print(f"salt:          {'set (HMAC-SHA256)' if result['salted'] else 'NOT SET (plain SHA-256): set VESTIGIA_SECRET_SALT'}")
        print(f"head hash:     {result['head_hash']}")
        for item in result["problems"][:20]:
            print(f"  PROBLEM {item['problem']} in {item['file']} at entry {item['index']} "
                  f"({item['event_id']}): {item['detail']}")
        if len(result["problems"]) > 20:
            print(f"  ... and {len(result['problems']) - 20} more")
        for step in result["clock_steps"][:5]:
            print(f"  CLOCK WARNING in {step['file']} at entry {step['index']} ({step['event_id']}): stamped {step['at']}, "
                  f"earlier than the entry before it ({step['before']}). The host clock stepped backward. "
                  "Times near here are unreliable. Content and order are not affected.")
        print("RESULT:        " + ("chain verifies across all ledger files" if result["valid"]
                                   else f"NOT VALID, {len(result['problems'])} problem(s)"))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
