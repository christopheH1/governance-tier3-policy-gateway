# Policy gateway: latency harness and ledger findings

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Status | Harness delivered as `governance-tier3-stage2-r4.tar.gz`. Not yet run on the Kali box |
| Build location | `~/Projects/governance-tier3` |
| Companions | `claude/POLICY_GATEWAY_STAGE2_BUILD.md`, `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md` |
| Purpose | Produce the Chapter 7 figures: latency per stage, and audit write time against ledger size |

## 1. What was built

| Item | Location | Role |
|---|---|---|
| Stage timing in the gateway | `gateway/app.py` | Each request records time spent per stage and returns it in a `Server-Timing` header |
| Harness | `tests/latency_harness.py` | Two measurements: `gateway` and `ledger-curve` |
| Run script | `scripts/latency-run.sh` | Runs the harness in the `e2e` container and saves results under `results/` |
| Ledger reset | `scripts/reset-ledger.sh` | Lab only. Deletes the ledger volume and starts an empty one, after a typed confirmation |

Stages reported by the gateway: `identity` (Tessera validation), `store` (Valkey), `policy` (OPA), `audit_decision` and `audit_result` (Vestigia writes before and after the tool), `tool`, and `total`. The harness adds the end-to-end time seen by the client.

Each run saves three files: full figures (`.json`), the report as shown (`.txt`), and what was measured (`.meta.txt`: ARTO commit, images, host, CPU count, memory).

## 2. Method, to state in Chapter 7

- Monotonic clock throughout. Client time excludes building the DPoP proof.
- The first 20 requests of each run are discarded as warm-up (`--warmup`).
- Load is closed-loop: N workers each send the next request when the last returns. The requests-a-second figure is what the stack sustained, not a rate imposed on it.
- Reported per run: p50, p95, p99, mean, standard deviation, minimum, maximum, count, and the number of requests with an unexpected outcome (excluded from the timings).
- Every run records ledger size before and after, since audit write time depends on it. Scenarios run one after another on a growing ledger, so rows are comparable only at similar ledger sizes, or after a reset.
- Load average, available memory, and swap use are recorded before and after. A run in which swap use grew is marked resource-contended.
- Scenarios: `deny_identity` (no credential), `deny_policy` (valid identity, intent does not fit the tool), `allow`, `refer_flag`, `refer_hold`.
- Both measurements add entries to the ledger. The gateway run leaves held calls that expire unanswered.

## 3. Verification

In the development container, services as plain processes on Python 3.11, not under Docker:

| Check | Result |
|---|---|
| End-to-end acceptance test with the instrumented gateway | 51 of 51 |
| Gateway measurement | Ran all five scenarios at 1 and 4 workers, no unexpected outcomes |
| Ledger curve | Ran 5,300 writes through the rotation point |
| `latency-run.sh` file handling | Checked against a stand-in for Docker. Not run against Docker |
| `reset-ledger.sh` | Syntax only. Not run |

## 4. Findings from the development container

These figures come from a two-CPU development container and indicate shape, not the values to cite. The Kali box figures are the ones for the paper.

### 4.1 Per-stage latency (single worker, ledger under 1,000 entries)

| Stage | Median |
|---|---|
| Policy decision (OPA) | about 1.7 ms |
| Identity validation (Tessera) | about 4 ms |
| Key-value store check | about 0.2 ms |
| Tool call (mock) | about 1.5 ms |
| Audit write (Vestigia), each | 20 to 60 ms, rising with ledger size |

Audit writes account for nearly all of the end-to-end time. An allowed call makes two.

### 4.2 Concurrency does not help

With four workers the sustained rate for allowed calls fell, from about 9 a second to about 5, and median end-to-end time rose from about 100 ms to about 830 ms. Ledger writes are serialised, so extra workers only queue. This run overlapped with the ledger curve run on the same two CPUs, so the size of the effect is unreliable, but the direction is expected and should be confirmed on the Kali box.

### 4.3 Audit write time against ledger size

| Ledger entries | Median write | p95 | Writes a second |
|---|---|---|---|
| 0 to 999 | 58 ms | 98 ms | 18.2 |
| 1,000 to 1,999 | 99 ms | 152 ms | 9.8 |
| 2,000 to 2,999 | 171 ms | 228 ms | 5.8 |
| 3,000 to 3,999 | 220 ms | 276 ms | 4.5 |
| 4,000 to 4,999 | 276 ms | 331 ms | 3.6 |
| 5,000 to 5,999 | 339 ms | 411 ms | 2.9 |
| 6,000 to 6,999 | 403 ms | 464 ms | 2.5 |
| 7,000 to 7,999 | 459 ms | 515 ms | 2.2 |
| 8,000 to 8,999 | 524 ms | 583 ms | 1.9 |
| 9,000 to 9,999 | 583 ms | 652 ms | 1.7 |
| After rotation | 56 ms | 88 ms | 13.9 |

Write time grows linearly, at roughly 0.06 ms per ledger entry in this environment. 5,300 writes were accepted and none refused, so the rate limit patch held throughout. Part of this run shared the CPUs with the gateway measurement.

### 4.4 Rotation at 10,000 entries

- Rotation happened as the ledger passed 10,000 entries. The full ledger was copied to `data/archives/` and a new ledger started. Write time fell back to the near-empty level.
- The chain does continue across the rotation: the first entry of the new ledger (`LEDGER_ROTATED`) carries the last hash of the archived ledger as its `previous_hash`.
- **Vestigia's own integrity check then reports the ledger as invalid, and keeps doing so.** `/integrity` returned `is_valid: false` with `BROKEN_CHAIN` at entry 0 ("expected ROOT") and `INVALID_GENESIS`, and `/health` returned `ledger_valid: false`. The verifier expects every ledger to begin with a genesis entry and does not recognise a rotation entry.
- Writes continued to be accepted after rotation.
- The rotation entry's own `integrity_hash` equals its `previous_hash`, so the content of that marker entry is not itself covered by a hash.

This is upstream behaviour at ARTO commit `b57be5e`. It is unrelated to the rate limit patch, which changes only that one line.

Consequences:

- After about 2,500 allowed calls (four entries each), the built-in integrity check can no longer demonstrate that the ledger is intact, even though the chain is in fact continuous.
- Anything that watches `ledger_valid` will raise a false alarm from that point on.
- The acceptance tests include "ledger integrity verifies", so `smoke_foundation.py` and `e2e_gateway.py` will fail that check once the ledger has rotated.
- Table 13 and Table 14 in Chapter 6 claim tamper detection through the hash chain. That holds within one ledger file. Across rotation it needs an independent verifier.

## 5. Decision needed: integrity checking across rotation

| Option | What it means |
|---|---|
| A: stay below rotation and report the limit | Reset the ledger before test campaigns, keep it under 10,000 entries, and state in the paper that the built-in check fails after rotation |
| B: independent verifier | A small own-code script that recomputes every hash from entry contents, across the archives and the current ledger, using the ledger salt. It proves continuity without relying on Vestigia's own check, which matches the paper's stated principle of evidence independent of the governed system |
| C: patch the upstream verifier | A second change to ARTO code, in the integrity check itself |

Recommended: B, together with reporting the finding upstream to Miracle Owolabi.

## 6. Next steps

| No. | Item |
|---|---|
| 1 | On the Kali box: rebuild the gateway image, rerun `e2e` as a regression check, then `./scripts/latency-run.sh gateway` |
| 2 | On the Kali box: `./scripts/reset-ledger.sh`, then `./scripts/latency-run.sh ledger-curve --max-minutes 90`. This takes the ledger through rotation, so reset it again afterwards |
| 3 | Decide section 5 |
| 4 | Rewrite the Chapter 7 hypotheses per stage, using the Kali figures |
| 5 | Stage 3: model path through LiteLLM, then telemetry |
