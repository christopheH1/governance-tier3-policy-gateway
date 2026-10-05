# Policy gateway: Stage 2 build notes (tool path)

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Revision | 4 (Stage 2 passed on the Kali box) |
| Status | Stage 1 and Stage 2 passed on the Kali box under Docker |
| Build location | `~/Projects/governance-tier3` |
| Companions | `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md` (revision 3), `claude/POLICY_GATEWAY_STAGE1_BUILD.md` |
| Supersedes | The status line and next steps of the Stage 1 build notes |

## 1. Stage 1 result on the Kali box

Run on 4 October 2026 under Docker Compose. Images built from ARTO commit `b57be5e`. The pinned image digests for Python, OPA, and Valkey resolved as recorded in `versions.lock`.

| Item | Result |
|---|---|
| Acceptance test | 23 of 23 checks passed |
| Key-value store | Valkey 9.0.6 confirmed. Tessera works against it with password authentication |
| Ledger write | 11.9 ms on a near-empty ledger |

This closes two items left open in the decision record: Valkey compatibility, and the Docker images and compose stack.

## 2. Decisions taken for Stage 2

| Item | Decision | Status |
|---|---|---|
| Provisioning model | A separate provisioning service holds the Tessera admin key | Decided by Christophe |
| Ledger rate limit | Option B: a documented one-line build-time patch makes Vestigia's rate limit configurable | Decided by Christophe. Built. See section 5.1 |
| Ledger write cost | Option A: accept it and report it. Chapter 7 reports latency per stage, with audit write time as a function of ledger size. No further patch, and the audit store stays as Vestigia's file ledger | Decided by Christophe. See section 5.3 |
| Agent authentication to the provisioner | Proof of possession: the operator enrols the agent's public key, and the agent signs each token request with the matching private key. No shared secret | Default chosen in the build, open to change |
| Token lifetime | 15 minutes, one token per tool | Default, open to change |
| Hold timeout | 900 seconds, set in the registry, with a per-tool override. Timeout results in deny | Default, open to change |
| Network layout | Three internal networks: agents, control, tools | Default, open to change |
| Tool destinations | Held in the registry and returned by OPA alongside the decision. The agent never supplies a URL | Built |

## 3. What Stage 2 adds

```
  agents network            control network                     tools network
  --------------            ---------------                     -------------
  agent ----> provisioner ----> Tessera (identity)
        |                 \---> Vestigia (audit ledger)
        |                 \---> OPA (registry)
        |
        +---> policy gateway ----> Tessera, OPA, Vestigia, Valkey
                        \------------------------------------------> mock tools
```

| Component | Location | Role |
|---|---|---|
| Policy gateway | `gateway/app.py` (about 400 lines) | `POST /v1/tools/invoke`, hold store, flag queue, reviewer endpoints, expiry sweeper |
| Provisioner | `provisioner/app.py` (about 190 lines) | Enrolment, per-tool token issuance, revocation |
| Mock tools | `mock-tools/app.py` | Stand-in tools that count their calls |
| Policy additions | `policies/tool_egress.rego` | `destination` and `hold_timeout_seconds` rules |
| Vestigia patch | `docker/patch_vestigia.py` | Rate limit read from the environment |
| Tests | `tests/`, `scripts/failclosed-test.sh` | End-to-end, isolation probe, fail-closed probe, ledger write measurement |

### 3.1 Gateway request sequence

1. Tessera validates the token, the DPoP proof (bound to the gateway URL), the tool, and revocation. Unreachable: deny.
2. Key-value store health check. Unreachable: deny.
3. OPA returns the decision, the tool destination, and the hold timeout. Unreachable or malformed: deny.
4. The decision is written to Vestigia before anything runs. Not recorded: deny.
5. Deny: refused with reasons. Allow: tool runs, result recorded. Flag: tool runs, entry added to the monitoring queue. Hold: pending record created, nothing runs.

The agent's role is read from the token only after Tessera has validated it. Refused credentials are recorded under the actor `unverified`, with the identity they claimed labelled as unproven.

### 3.2 Hold rules as built

- The pending record stores a SHA-256 fingerprint of agent, tool, parameters, purpose, and task. A resubmission must match it exactly.
- State changes are compare-and-set in the key-value store: pending to approved, rejected, or expired, and approved to consumed. An approval is single use.
- A sweeper expires unanswered holds. Expiry is also checked on every lookup.
- A review decision that cannot be written to the ledger is rolled back to pending.
- Ledger events: `HOLD_CREATED`, `HOLD_APPROVED`, `HOLD_REJECTED`, `HOLD_EXPIRED`, `HOLD_RELEASED`.

## 4. What has been verified, and how

### 4.1 In the development container

All services ran as plain processes on Python 3.11 with the pinned packages, against Redis 7.0.15 and OPA 1.19.1. Not under Docker.

| Test | Result |
|---|---|
| Policy tests | 18 of 18, `opa check --strict` clean |
| End-to-end acceptance test (`e2e_gateway.py`) | 51 of 51 (run before the patch was added) |
| Fail-closed probe | 9 of 9: OPA, Tessera, Vestigia, and the key-value store each stopped in turn. Every call denied, no tool reached, service resumed on restore |
| Vestigia patch | Applies exactly once and refuses to apply twice. Unset: 37 of 60 burst writes refused, as upstream. Set to 1000 a second: 1,500 of 1,500 writes accepted, ledger integrity valid |

### 4.2 On the Kali box under Docker (4 October 2026)

| Test | Result | Evidence |
|---|---|---|
| Stack build and start, three networks | Running | Output seen |
| Agent isolation probe | 8 of 8: an agent reaches only the gateway and the provisioner | Output seen |
| Vestigia patch under Docker | 200 of 200 burst writes accepted, none refused | Output seen |
| Fail-closed test, corrected script | Passed: OPA, Tessera, Vestigia, and Valkey each stopped in turn | Reported by Christophe. Output not captured in these notes |
| End-to-end acceptance test | Passed | Reported by Christophe. Output not captured in these notes |

For the paper, keep the full output of both reported runs together with the image digests and the ARTO commit, so the results can be cited with evidence. Rerun and save with:

```bash
./scripts/failclosed-test.sh 2>&1 | tee results-failclosed-$(date +%Y%m%d).txt
docker compose --profile test run --rm e2e 2>&1 | tee results-e2e-$(date +%Y%m%d).txt
```

### 4.3 Script defect, corrected

The first version of `failclosed-test.sh` read its list of services from its own input, and `docker compose run` consumed the rest of that input on the first pass. The loop ended after OPA and, with nothing failed, printed "passed". The corrected script iterates over an array, closes input on every Docker call, and counts checks: it fails unless nine ran and passed. Any "fail-closed test passed" without "9 of 9 checks passed" is not evidence and must not be cited.

## 5. The ledger: rate limit patch and write time

### 5.1 The patch (p1)

Vestigia fixes its API rate limit in source at 10 requests a second with a burst of 20 per client address (`vestigia/api_server.py`, line 114). `docker/patch_vestigia.py` replaces that one line at image build so the values come from `VESTIGIA_RATE_LIMIT_RPS` and `VESTIGIA_RATE_LIMIT_BURST`, with the upstream values as defaults.

- `vendor/ARTO` stays unmodified. `bootstrap.sh` still verifies that.
- The build fails if the line is not found exactly once.
- Build setting: 200 a second, burst 400. Image tag `governance-tier3/vestigia:b57be5e-p1`.
- Recorded in `versions.lock`. To be reported in the paper and raised upstream with Miracle Owolabi.

### 5.2 What the patch revealed

With the rate limit out of the way, the ledger itself is the ceiling. Every write rewrites the whole ledger file, so write time grows in step with ledger size.

| Where | Ledger size | Median write time | Sequential writes a second |
|---|---|---|---|
| Development container | Near-empty | about 9 to 15 ms | about 54 over the first 200 writes |
| Development container | About 400 entries | about 26 ms | |
| Development container | About 3,000 entries (2.2 MB) | about 147 ms | about 7 |
| Development container | Average over 1,500 writes | | 12 |
| Kali box, Docker | 445 entries at start | 33 ms | 23.3 over 200 writes |
| Kali box, Docker | About 845 entries at end | 52 ms (p95 52.8 ms) | |

Single client in each case.

Consequences:

- Rotation would cap the cost, but the API server builds its ledger with the engine's fixed default of 10,000 entries. `VESTIGIA_MAX_ENTRIES` is not read by the API server. Extrapolating the measurements, a write near 10,000 entries would take about half a second.
- An allowed call writes two events, and each event adds two entries. Sustained synchronous throughput is therefore a few allowed calls a second, falling as the ledger fills. On the Kali box at about 450 to 850 entries it is roughly ten allowed calls a second.
- The Chapter 7 plan of up to 500 requests a second cannot be met with synchronous Vestigia audit, with or without the patch.

### 5.3 Decision and what it means for Chapter 7

Christophe chose to accept the write cost and report it.

- Chapter 7 reports latency per stage: identity validation (Tessera), policy decision (OPA), audit write (Vestigia), and the end-to-end figure.
- Audit write time is reported as a function of ledger size, as a measured curve.
- Load levels are scaled to what the ledger sustains. The 500 requests a second target is dropped for the audited path, and the paper says why.
- The hypothesis "Tier 3 latency p99 below 0.1 ms" is replaced by per-stage hypotheses, with audit write stated separately.
- A configurable rotation threshold, or a database-backed chain, is presented as what Profile 2 and Profile 1 would need, not as part of the Profile 3 baseline.
- This fits the Profile 3 audience: a low-volume, limited-budget organisation making a few audited tool calls a second.

## 6. Other findings

1. **Each ledger write produces two entries**, since Vestigia records its own API requests.
2. **Tessera carries the role in the token** when the provisioner supplies it, and restricts it through `allowed_roles` at registration. The gateway therefore needs no role store of its own.
3. **Token issuance is limited to 100 an hour per agent** in Tessera's source. Validation is not limited. Tokens are reusable until expiry, with a fresh proof per call.
4. **Tokens survive a Tessera restart**, because the registry, revocation list, and signing secret persist.
5. **Fail closed holds in practice.** With the ledger down, or refusing writes, an otherwise allowed call is denied before the tool is called.

## 7. Limits to state in the paper

- One patch is applied to upstream ARTO code (section 5.1).
- Audit throughput is bounded by the file ledger (section 5.2).
- Reviewer and operator access each use one shared key. The reviewer's name is recorded as given, not proven.
- Reviewer endpoints are served by the gateway and are reachable from the agents network, protected by the key only.
- One gateway instance and one Tessera instance.
- External tools are simulated by the mock tools service. No egress network exists yet.
- A tool that ran but whose result could not be written to the ledger is logged as an error, since the action has already happened.
- Parameters are stored in the ledger in full. Sensitive parameters would need redaction.

## 8. Open items

| No. | Item |
|---|---|
| 1 | Save the full output of the fail-closed and end-to-end runs as evidence (section 4.2) |
| 2 | Latency harness for Chapter 7: per-stage timings, and audit write time against ledger size up to the 10,000-entry rotation point |
| 3 | Stage 3: model path through LiteLLM (MIT tree) with an egress network attached to the gateway only, then telemetry |
| 4 | Confirm or change the Stage 2 defaults in section 2 |
| 5 | Replace the example registry with the real purpose catalogue and tool classification, and remove the `test_fixture` entries |
| 6 | Dependency audit of ARTO's pinned packages |
| 7 | Chapter 6 and Chapter 7 corrections: those in the decision record, section 12, the provisioner as a distinct component, and the changes in section 5.3 |
