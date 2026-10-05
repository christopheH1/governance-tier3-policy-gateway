"""Latency harness for Chapter 7.

Two measurements:

  gateway        Drives the policy gateway with each kind of outcome and reports, per
                 stage, how long a request spent in identity validation, the policy
                 decision, the audit write, and the tool call, plus the end-to-end time
                 seen by the client.

  ledger-curve   Writes events straight to the ledger and reports write time against
                 ledger size, up to and past the rotation point.

The human-readable report goes to standard error. The full results go to standard
output as JSON, so scripts/latency-run.sh can save both.

Method notes, which belong in the paper alongside the figures:
  - Timings use a monotonic clock. Client time excludes building the DPoP proof.
  - The first --warmup requests of each run are discarded.
  - Load is closed-loop: N workers each send the next request when the last returns.
    Throughput is therefore what the stack sustained, not a rate imposed on it.
  - Stage times are measured inside the gateway and returned in a Server-Timing header.
  - Every run records ledger size before and after, because audit write time depends on it.
  - Both measurements add entries to the ledger.
"""
import argparse
import json
import math
import os
import platform
import statistics
import sys
import threading
import time
import uuid

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

GATEWAY = os.environ.get("GATEWAY_URL", "http://policy-gateway:9090")
PROVISIONER = os.environ.get("PROVISIONER_URL", "http://provisioner:9080")
VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")
INVOKE_URL = f"{GATEWAY}/v1/tools/invoke"
CHAT_URL = f"{GATEWAY}/v1/models/chat"
TOKEN_URL = f"{PROVISIONER}/v1/tokens"
STAGES = ["identity", "store", "policy", "audit_decision", "tool", "model", "audit_result", "total"]


def say(text=""):
    print(text, file=sys.stderr, flush=True)


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def summarise(values):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50": round(percentile(values, 0.50), 3), "p95": round(percentile(values, 0.95), 3),
        "p99": round(percentile(values, 0.99), 3), "mean": round(statistics.fmean(values), 3),
        "sd": round(statistics.pstdev(values), 3), "min": round(min(values), 3), "max": round(max(values), 3),
    }


def host_state():
    """Load and memory as seen from this container. Swap in use marks a run as resource-contended."""
    state = {}
    try:
        with open("/proc/loadavg") as handle:
            state["loadavg_1m"] = float(handle.read().split()[0])
        with open("/proc/meminfo") as handle:
            info = {line.split(":")[0]: int(line.split()[1]) for line in handle if ":" in line}
        state["mem_available_mb"] = info.get("MemAvailable", 0) // 1024
        state["swap_used_mb"] = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return state


def ledger_entries():
    try:
        return requests.get(f"{VESTIGIA}/health", timeout=60).json().get("total_events")
    except (requests.RequestException, ValueError):
        return None


# --- gateway measurement ----------------------------------------------------------
class Agent:
    def __init__(self, role):
        self.agent_id = f"latency-{role}-{uuid.uuid4().hex[:8]}"
        self.role = role
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        numbers = self.key.public_key().public_numbers()
        b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
        self.tokens = {}

    def proof(self, url):
        claims = {"htu": url, "htm": "POST", "iat": int(time.time()), "jti": uuid.uuid4().hex}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": self.jwk})

    def enrol(self):
        operator = {"Authorization": f"Bearer {os.environ['PROVISIONER_OPERATOR_KEY']}"}
        requests.post(f"{PROVISIONER}/v1/agents", headers=operator, timeout=30, json={
            "agent_id": self.agent_id, "role": self.role, "owner": "latency-harness",
            "public_key_pem": self.public_pem}).raise_for_status()

    def token(self, tool):
        """Tokens last 15 minutes. Fetch a new one after 10, well inside the issuance limit."""
        held = self.tokens.get(tool)
        if not held or time.time() - held[1] > 600:
            resp = requests.post(TOKEN_URL, headers={"DPoP": self.proof(TOKEN_URL)}, timeout=30,
                                 json={"agent_id": self.agent_id, "tool": tool})
            resp.raise_for_status()
            self.tokens[tool] = (resp.json()["token"], time.time())
        return self.tokens[tool][0]


def scenarios(analyst, processor, task):
    reporting = {"purpose": "sales_reporting", "task_id": task}
    return {
        "allow": dict(agent=analyst, tool="database_read", params={"table": "sales", "limit": 3},
                      intent=reporting, expect="allow", credential=True),
        "deny_policy": dict(agent=analyst, tool="database_read", params={"limit": 3},
                            intent={"purpose": "market_research", "task_id": task}, expect="deny", credential=True),
        "deny_identity": dict(agent=analyst, tool="database_read", params={"limit": 3},
                              intent=reporting, expect="deny", credential=False),
        "refer_flag": dict(agent=analyst, tool="database_read", params={"table": "sales", "limit": 50000},
                           intent=reporting, expect="refer", credential=True),
        "refer_hold": dict(agent=processor, tool="data_export", params={"dataset": "sales_q4"},
                           intent={"purpose": "data_export", "task_id": task}, expect="refer", credential=True),
        # The model path, against the mock model: gateway overhead without provider time.
        "model_allow": dict(agent=analyst, tool="model_access", model="mock-model", intent=reporting,
                            expect="allow", credential=True),
    }


def parse_server_timing(header):
    stages = {}
    for part in (header or "").split(","):
        name, _, rest = part.strip().partition(";dur=")
        try:
            stages[name] = float(rest)
        except ValueError:
            pass
    return stages


def run_scenario(name, spec, count, warmup, workers):
    agent = spec["agent"]
    samples, lock = [], threading.Lock()
    remaining = [count + warmup]
    started_at = [None]

    def worker():
        session = requests.Session()
        while True:
            with lock:
                if remaining[0] <= 0:
                    return
                remaining[0] -= 1
                index = count + warmup - remaining[0]
                if index == warmup + 1:
                    started_at[0] = time.perf_counter()
            url = CHAT_URL if "model" in spec else INVOKE_URL
            headers = {}
            if spec["credential"]:
                headers = {"Authorization": f"DPoP {agent.token(spec['tool'])}", "DPoP": agent.proof(url)}
            if "model" in spec:
                payload = {"intent": spec["intent"], "model": spec["model"], "max_tokens": 50,
                           "messages": [{"role": "user", "content": "Summarise the quarter in one line."}]}
            else:
                # A unique parameter keeps hold fingerprints distinct. It is ignored by the mock tools.
                payload = {"intent": spec["intent"],
                           "tool": {"name": spec["tool"], "params": {**spec["params"], "request": uuid.uuid4().hex}}}
            begin = time.perf_counter()
            try:
                resp = session.post(url, headers=headers, json=payload, timeout=60)
                elapsed = (time.perf_counter() - begin) * 1000
                outcome = resp.json().get("outcome")
                stages = parse_server_timing(resp.headers.get("Server-Timing"))
            except (requests.RequestException, ValueError):
                elapsed, outcome, stages = (time.perf_counter() - begin) * 1000, "no answer", {}
            if index > warmup:
                with lock:
                    samples.append((elapsed, outcome, stages))

    before_entries, before_host = ledger_entries(), host_state()
    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    duration = time.perf_counter() - (started_at[0] or time.perf_counter())
    after_entries, after_host = ledger_entries(), host_state()

    good = [s for s in samples if s[1] == spec["expect"]]
    result = {
        "scenario": name, "workers": workers, "requests": len(samples), "warmup_discarded": warmup,
        "unexpected_outcomes": len(samples) - len(good),
        "throughput_per_second": round(len(samples) / duration, 2) if duration > 0 else None,
        "ledger_entries_before": before_entries, "ledger_entries_after": after_entries,
        "host_before": before_host, "host_after": after_host,
        "resource_contended": bool(after_host.get("swap_used_mb", 0) > before_host.get("swap_used_mb", 0)),
        "client_end_to_end_ms": summarise([s[0] for s in good]),
        "stages_ms": {stage: summarise([s[2][stage] for s in good if stage in s[2]]) for stage in STAGES},
    }
    result["stages_ms"] = {k: v for k, v in result["stages_ms"].items() if v.get("n")}
    return result


def print_gateway_report(results):
    say()
    say(f"{'scenario':14} {'wk':>2} {'n':>5} {'bad':>3} {'req/s':>6} {'ledger':>13}  "
        f"{'end-to-end ms':>22}  stage medians, ms")
    say(f"{'':14} {'':>2} {'':>5} {'':>3} {'':>6} {'before>after':>13}  {'p50':>6} {'p95':>7} {'p99':>7}")
    for r in results:
        e2e = r["client_end_to_end_ms"]
        stages = "  ".join(f"{name}={r['stages_ms'][name]['p50']:.1f}" for name in STAGES
                           if name in r["stages_ms"] and name != "total")
        ledger = f"{r['ledger_entries_before']}>{r['ledger_entries_after']}"
        mark = " *" if r["resource_contended"] else ""
        if not e2e.get("n"):
            say(f"{r['scenario']:14} {r['workers']:>2} {r['requests']:>5} {r['unexpected_outcomes']:>3}  no usable samples")
            continue
        say(f"{r['scenario']:14} {r['workers']:>2} {r['requests']:>5} {r['unexpected_outcomes']:>3} "
            f"{r['throughput_per_second']:>6} {ledger:>13}  {e2e['p50']:>6.1f} {e2e['p95']:>7.1f} {e2e['p99']:>7.1f}  "
            f"{stages}{mark}")
    say()
    say("bad = requests whose outcome was not the expected one (excluded from the timings).")
    say("* = swap use grew during the run: treat as resource-contended.")


def measure_gateway(args):
    task = f"latency-{uuid.uuid4().hex[:8]}"
    analyst, processor = Agent("analyst"), Agent("data_processor")
    for agent in (analyst, processor):
        agent.enrol()
    available = scenarios(analyst, processor, task)
    chosen = [name.strip() for name in args.scenarios.split(",")]
    results = []
    for workers in [int(value) for value in args.workers.split(",")]:
        for name in chosen:
            count = min(args.requests, args.max_holds) if name == "refer_hold" else args.requests
            say(f"running {name} with {workers} worker(s), {count} requests ...")
            results.append(run_scenario(name, available[name], count, args.warmup, workers))
    print_gateway_report(results)
    return {"measurement": "gateway", "results": results}


# --- ledger curve -----------------------------------------------------------------
def measure_ledger_curve(args):
    headers = {"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"}
    event = {"actor_id": "latency-harness", "action_type": "MEASUREMENT", "status": "SUCCESS",
             "evidence": {"tool": "database_read", "params": {"table": "sales", "limit": 3},
                          "intent": {"purpose": "sales_reporting", "task_id": "ledger-curve"},
                          "outcome": "allow", "reasons": ["all_constraints_satisfied"]}}
    session = requests.Session()
    entries = ledger_entries()
    start_entries, deadline = entries, time.time() + args.max_minutes * 60
    samples, refused, rotations, writes_after_rotation = [], 0, [], 0
    say(f"ledger entries at start: {entries}. Target: {args.target_entries}. Limit: {args.max_minutes} minutes.")
    while time.time() < deadline:
        begin = time.perf_counter()
        status = session.post(f"{VESTIGIA}/events", json=event, headers=headers, timeout=120).status_code
        elapsed = (time.perf_counter() - begin) * 1000
        if status != 201:
            refused += 1
            continue
        entries = (entries or 0) + 2          # each write adds the event and Vestigia's own request record
        samples.append((entries, elapsed))
        if rotations:
            writes_after_rotation += 1
        if len(samples) % 100 == 0:
            actual = ledger_entries()
            if actual is not None:
                if actual < entries - 500:
                    rotations.append({"after_writes": len(samples), "entries_before": entries, "entries_after": actual})
                    say(f"  rotation observed: ledger went from about {entries} to {actual} entries")
                entries = actual
            say(f"  {len(samples)} writes, about {entries} entries, last write {elapsed:.0f} ms")
        if rotations and writes_after_rotation >= args.after_rotation:
            break
        if not rotations and entries >= args.target_entries:
            break

    begin = time.perf_counter()
    try:
        integrity = session.get(f"{VESTIGIA}/integrity", headers=headers, timeout=600).json()
    except (requests.RequestException, ValueError):
        integrity = {}
    integrity_ms = (time.perf_counter() - begin) * 1000

    buckets = {}
    for size, elapsed in samples:
        buckets.setdefault((size // args.bucket) * args.bucket, []).append(elapsed)
    curve = [{"entries_from": low, "entries_to": low + args.bucket - 1, **summarise(values)}
             for low, values in sorted(buckets.items())]

    say()
    say(f"{'ledger entries':>17} {'n':>5} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'writes/s':>9}")
    for row in curve:
        say(f"{row['entries_from']:>7}-{row['entries_to']:<9} {row['n']:>5} {row['p50']:>8.1f} {row['p95']:>8.1f} "
            f"{row['max']:>8.1f} {1000 / row['mean']:>9.1f}")
    say()
    say(f"writes accepted: {len(samples)}, refused: {refused}, rotations observed: {len(rotations)}")
    say(f"integrity check afterwards: valid={integrity.get('is_valid')}, "
        f"{integrity.get('total_entries')} entries, took {integrity_ms / 1000:.1f} s")
    if time.time() >= deadline:
        say("stopped at the time limit before reaching the target")
    return {"measurement": "ledger-curve", "entries_at_start": start_entries, "writes_accepted": len(samples),
            "writes_refused": refused, "rotations": rotations, "curve": curve,
            "integrity_after": {"is_valid": integrity.get("is_valid"),
                                "total_entries": integrity.get("total_entries"),
                                "seconds": round(integrity_ms / 1000, 2)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    gateway = modes.add_parser("gateway")
    gateway.add_argument("--requests", type=int, default=200, help="measured requests per scenario")
    gateway.add_argument("--warmup", type=int, default=20, help="requests discarded at the start of each run")
    gateway.add_argument("--workers", default="1,4", help="closed-loop concurrency levels, comma-separated")
    gateway.add_argument("--scenarios", default="deny_identity,deny_policy,allow,refer_flag,refer_hold,model_allow")
    gateway.add_argument("--max-holds", type=int, default=50, help="cap on held calls created per run")
    curve = modes.add_parser("ledger-curve")
    curve.add_argument("--target-entries", type=int, default=10500)
    curve.add_argument("--bucket", type=int, default=1000, help="ledger size bucket for the report")
    curve.add_argument("--after-rotation", type=int, default=300, help="writes to keep measuring after a rotation")
    curve.add_argument("--max-minutes", type=float, default=60)
    args = parser.parse_args()

    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    output = measure_gateway(args) if args.mode == "gateway" else measure_ledger_curve(args)
    output.update({"started": started, "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "parameters": vars(args), "python": platform.python_version(),
                   "machine": platform.machine(), "cpu_count": os.cpu_count()})
    json.dump(output, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
