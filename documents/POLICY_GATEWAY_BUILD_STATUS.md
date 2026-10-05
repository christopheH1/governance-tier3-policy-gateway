# Policy gateway: build status

| Field | Value |
|---|---|
| Last updated | 5 October 2026, 11:50 (Singapore) |
| Build location | `~/Projects/governance-tier3` on the Kali box |
| Current archive | `governance-tier3-stage6-r8.tar.gz` (delivered: the words of a flagged request are kept in the ledger). Revision 7 runs on the Kali box: individual console accounts, and the person recorded on every call |
| Purpose of this page | One-page index of where the build stands. Update this page first, then the detailed notes |

## 1. Where things stand

| Stage | Scope | Status on the Kali box (Docker) |
|---|---|---|
| 1 | Identity (Tessera), audit (Vestigia), policy (OPA), key-value store (Valkey) | Passed |
| 2 | Policy gateway, provisioner, tool path, hold and flag, three networks | Passed |
| Harness | Per-stage latency and ledger write curve | Both run. Results in section 3 |
| 3 | Model path, model adapter, egress network, independent ledger verifier | Passed with the mock model. Provider call made by Christophe, result not yet seen |
| 4 | Telemetry: OpenTelemetry Collector and gateway instrumentation | Passed |
| 5 | Two agents for the Research Operations scenario, and a web console for operator and reviewer | Passed with the scripted model (18 of 18). Console not yet confirmed in a browser. Real provider not yet tried in the agent loop |
| 6 | Bridge for workflow applications (OpenAI chat format and MCP), LangGraph workflow, local model on Ollama, purpose-fit check with its own checking model. Flowise evaluated and removed | Reported as successful by Christophe: bridge and workflow tests, local model, Flowise start-up, a real workflow run. Outputs not yet seen. Revision 2 (reply from the console) seen working. First real runs showed invented figures from the local model and off-purpose requests being allowed (Stage 6 notes, sections 8 and 9). Revision 3 (purpose-fit check) passed its checks and flagged four of four off-purpose requests with the real local model. A clock step from dual-booting makes Vestigia report the ledger invalid although the chain verifies (Stage 6 notes, section 11). Revision 5 (checking model on the control network) runs: model path test 27 of 27, isolation probe 12 of 12. Flowise removed (revision 6), confirmed. The control-plane checker flagged an off-purpose request on the Kali box |

The build covers the Profile 3 scope of the paper: the Observability Plane plus Tier 3. All six stages run on the Kali box.

## 2. Results on the Kali box

All on 4 October 2026, ARTO commit `b57be5e`, Vestigia image `b57be5e-p1`. Host: 16 CPUs, 15.0 GB memory, Linux 7.0.12 (Kali), Docker 29.7.2.

| Test | Result | Evidence |
|---|---|---|
| Foundation test (`smoke_foundation.py`) | 23 of 23 | Output seen |
| Tool path test (`e2e_gateway.py`) | 51 of 51, rerun after Stage 3 and after Stage 4 | Output seen (final line) |
| Model path test (`e2e_models.py`), mock model | 20 of 20, rerun after Stage 4 | Output seen |
| Agent isolation probe | 10 of 10 at Stage 4, after a host reboot | Output seen |
| Fail-closed test, all four dependencies | Passed | Reported by Christophe |
| Ledger verifier | Chain verifies at 505 entries, and again at 35 entries after a ledger reset and a host reboot | Output seen |
| Telemetry check | "telemetry looks complete". 37 requests counted, 29 traces kept: all 21 denials, all 5 referrals, 3 of 11 allowed | Output seen |
| Restart persistence | After a host reboot all nine services returned healthy without intervention and the ledger chain verifies | Output seen. A light test: the ledger held 35 entries |
| Gateway latency | Two runs, about 4,150 measured requests, no unexpected outcome and no refused write | Output seen. Saved in `results/` |
| Agents through the console (`e2e_agents.py`), scripted model | 18 of 18. All eleven services healthy | Output seen |
| Ledger curve, five minutes from empty | 27 ms rising to 208 ms at 4,000 to 5,000 entries | Output seen. Saved in `results/` |

Current head hash of the ledger (35 entries, after the reset): `48ee91ce94a83813fdd851b7fce9929af1f025980a81389eb4e98a0c6e39ebb3`. The head hash recorded earlier belonged to the ledger that was reset.

## 3. Gateway latency on the Kali box

Run of 11:13, `results/latency-gateway-*.txt`. 200 measured requests per row (50 for holds), 20 discarded as warm-up, closed-loop load. No row was marked resource-contended.

### 3.1 One worker

| Scenario | Ledger entries | End to end p50 / p95 / p99 (ms) | Requests a second | Identity | Policy | Audit, each write |
|---|---|---|---|---|---|---|
| Deny, no credential | 599 to 1,234 | 60.3 / 79.3 / 93.1 | 16.5 | not called | 1.2 | 57.2 |
| Deny by policy | 1,235 to 1,682 | 72.0 / 94.7 / 105.0 | 13.6 | 2.9 | 1.1 | 66.0 |
| Allow (tool) | 1,683 to 2,571 | 199.1 / 238.7 / 258.2 | 5.0 | 2.9 | 1.1 | 94.3 and 95.3 |
| Refer and flag | 2,572 to 3,457 | 276.9 / 324.9 / 351.8 | 3.6 | 2.9 | 1.1 | 131.0 and 133.4 |
| Refer and hold | 3,458 to 3,606 | 155.5 / 189.4 / 218.5 | 6.2 | 2.8 | 1.1 | 149.3 |
| Allow (model, mock) | 3,607 to 4,495 | 371.4 / 415.6 / 485.9 | 2.7 | 2.9 | 1.1 | 179.0 and 179.5 |

Stage figures are medians in milliseconds. Key-value store: 0.1 to 0.7 ms. Tool call (mock): 1.1 ms. Model call (mock): 4.2 ms.

### 3.2 Four workers

| Scenario | Ledger entries | End to end p50 (ms) | Requests a second | Audit, each write (median) |
|---|---|---|---|---|
| Deny, no credential | 4,496 to 5,161 | 1,276.5 | 3.1 | 1,273.1 |
| Deny by policy | 5,162 to 5,607 | 983.7 | 4.0 | 975.4 |
| Allow (tool) | 5,608 to 6,499 | 2,316.7 | 1.7 | 1,156.3 and 1,170.1 |
| Refer and flag | 6,500 to 7,394 | 2,697.4 | 1.5 | 1,317.4 and 1,322.5 |
| Refer and hold | 7,396 to 7,538 | 1,377.9 | 2.7 | 1,370.7 |
| Allow (model, mock) | 7,539 to 8,435 | 3,087.0 | 1.3 | 1,530.7 and 1,507.1 |

### 3.3 What the figures show

1. **Enforcement without audit costs about 4 to 5 ms.** Identity validation (2.9 ms), the policy decision (1.1 ms), and the key-value store (under 1 ms) are stable across every scenario and ledger size.
2. **The audit write is 95 per cent or more of the end-to-end time.** For an allowed tool call, 189.6 ms of the 199.1 ms median is two ledger writes.
3. **Audit write time tracks ledger size**, at about 0.045 ms per entry, matching the separate ledger curve.
4. **More workers make things worse, not better.** Ledger writes are serialised, so with four workers each request waits behind the others: the audit stage takes about four times a single write, and throughput falls. This confirms on the Kali box what the development container suggested.
5. **Nothing failed.** No request had an unexpected outcome, and no ledger write was refused, in either run.
6. **The Chapter 6 claim of under 0.1 ms for the OPA sidecar does not hold.** The median policy decision is 1.1 ms including its network hop. The earlier workplan hypothesis of "OPA p99 below 1 ms" also fails on the median alone.
7. The second run ended at 8,435 ledger entries. This confirms the earlier inference that the 375 to 590 ms audit writes seen in the Stage 4 traces came from a ledger near the rotation point. The ledger was reset afterwards.

The first run, at 09:56, predates the model path and started from a larger ledger (1,507 entries). Its figures are consistent with the second: for example allow at 260.9 ms median on a ledger of 2,600 to 3,489 entries.

Per-stage p95 and p99 are in the `.json` files, not in the text report.

## 4. Stage 4: telemetry, as built

| Item | Location | Role |
|---|---|---|
| Collector | `otel-collector` service, `observability/otel-collector.yaml` | Receives OTLP over HTTP on the control network. Writes traces and metrics as JSON lines to the `telemetry-data` volume. Exposes metrics for scraping on port 8889 |
| Gateway instrumentation | `gateway/telemetry.py` | OpenTelemetry SDK 1.45.0. One trace per request, one child span per stage, a decision counter, and a duration histogram |
| Summary tool | `tools/telemetry_summary.py`, `telemetry-show` service | Reads the collector's files with no network. `--check` verifies telemetry is complete |

Design points: telemetry fails open and audit fails closed. The trace identifier is shared with the ledger. Sampling is done in the collector (all denials, referrals, and failures, one in ten allowed). Tool parameters and prompt content are not sent. A policy denial is an outcome, not an error. Only the gateway is instrumented.

The fail-open case (collector stopped, tests still pass) was verified in the development container and has not been repeated on the Kali box.

Differences from Chapter 6 to record: the collector is a single service, not a sidecar DaemonSet. Exporters are files and a Prometheus scrape endpoint, not Langfuse, Jaeger, or Splunk. Audit goes from the gateway straight to Vestigia. The Table 16 data flows from Tessera, OPA, and LiteLLM into OpenTelemetry do not exist in this build.

## 5. Not yet done

| No. | Item | Command or note |
|---|---|---|
| 1 | Result of the real provider call | Run by Christophe, output not yet seen |
| 2 | Saved output of the fail-closed and end-to-end runs, as evidence | `... 2>&1 | tee results/<name>.txt` |
| 3 | Full ledger curve through rotation, on the Kali box | Optional. The rotation finding rests on the development container run |
| 4 | Fail-open telemetry check on the Kali box | Optional. `docker compose stop otel-collector`, run both acceptance tests, start it again |
| 5 | Console opened in a browser at `http://127.0.0.1:8088` | The test reaches the console over the agents network, so the published port and the `ui` network setting are still unconfirmed |
| 6 | An agent task with `claude-haiku` from the console | The agent loop with real tool calling is untested |

## 6. Decisions in force

| Topic | Decision |
|---|---|
| Name | Policy gateway |
| Outcomes | Deny, refer (hold or flag), allow. Model path: deny or allow |
| Audit store | Vestigia's file ledger, accepted with its write cost |
| Ledger rate limit | One build-time patch makes it configurable (200 a second in this build) |
| Integrity across rotation | Independent verifier, `tools/verify_ledger.py` |
| Provisioning | Separate provisioner holds the Tessera admin key |
| Provider | Anthropic, through an own adapter on the LiteLLM library. The LiteLLM proxy is not used |
| Telemetry | OpenTelemetry Collector, fail open, sampled in the collector, files as the store |
| Chapter 7 | Latency per stage. Audit write time as a function of ledger size. The 500 requests a second target is dropped for the audited path |

## 7. Findings to report upstream (Miracle Owolabi)

1. Vestigia's API rate limit is fixed in source at 10 requests a second.
2. Vestigia's integrity check reports the ledger invalid after rotation, although the chain is continuous.
3. The rotation threshold is fixed at 10,000 entries in the API server. `VESTIGIA_MAX_ENTRIES` is not read.
4. Each write rewrites the whole ledger file, so write time grows linearly with ledger size, and writes are serialised.
5. Tessera's revoke, validate, and agent-list endpoints accept unauthenticated calls in strict mode.
6. Tessera's registration and token issuance are unauthenticated unless `TESSERA_REQUIRE_REGISTRATION_AUTH` is set.

## 8. Open work, in suggested order

| No. | Item |
|---|---|
| 1 | Done: corrections consolidated in `claude/PAPER_CORRECTIONS_BY_SECTION.md`. Stage 5 adds three items (Stage 5 notes, section 7) |
| 2 | Replace the example registry with real purposes, tools, models, and limits |
| 3 | Section 5, item 1 |
| 4 | Restrict the model adapter's egress to the provider |
| 5 | Dependency audit of ARTO's pinned packages, the adapter's set, and the services' set |
| 6 | Key hygiene: the Anthropic key pasted into the working session on 4 October should be revoked and replaced |
| 7 | Delegation between agents: Stage 6 gives a hand-over under one task identifier. A delegated credential with a depth limit is not built |
| 8 | Console hardening: separate operator and reviewer identities, console off the agents network |

## 9. Detailed notes

| Document | Covers |
|---|---|
| `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md` | Design decisions, ARTO source findings, Chapter 6 corrections |
| `claude/POLICY_GATEWAY_STAGE1_BUILD.md` | Foundation services |
| `claude/POLICY_GATEWAY_STAGE2_BUILD.md` | Tool path, provisioner, rate limit patch, ledger write cost |
| `claude/POLICY_GATEWAY_LATENCY_HARNESS.md` | Harness method, development container figures, rotation finding |
| `claude/POLICY_GATEWAY_STAGE3_BUILD.md` | Model path, adapter, verifier, Kali ledger curve |
| `claude/POLICY_GATEWAY_STAGE5_BUILD.md` | Agents, console, scripted model, audit trail demonstration, limits |
| `claude/POLICY_GATEWAY_STAGE6_BUILD.md` | Workflow bridge, LangGraph, Flowise, local model, decision to keep the policy gateway |
| `claude/PAPER_CORRECTIONS_BY_SECTION.md` | Corrections to the paper, by section |

Some status lines in the detailed notes predate the Kali runs. This page is the current status.
