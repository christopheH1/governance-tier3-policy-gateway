# Policy gateway: build status

| Field | Value |
|---|---|
| Last updated | 4 October 2026, 11:35 (Singapore) |
| Build location | `~/Projects/governance-tier3` on the Kali box |
| Current archive | `governance-tier3-stage4.tar.gz` |
| Purpose of this page | One-page index of where the build stands. Update this page first, then the detailed notes |

## 1. Where things stand

| Stage | Scope | Status on the Kali box (Docker) |
|---|---|---|
| 1 | Identity (Tessera), audit (Vestigia), policy (OPA), key-value store (Valkey) | Passed |
| 2 | Policy gateway, provisioner, tool path, hold and flag, three networks | Passed |
| Harness | Per-stage latency and ledger write curve | Ledger curve run. Gateway latency run in progress, result not yet seen |
| 3 | Model path, model adapter, egress network, independent ledger verifier | Passed with the mock model. Provider call in progress, result not yet seen |
| 4 | Telemetry: OpenTelemetry Collector and gateway instrumentation | Delivered. Not yet run under Docker |

With Stage 4 the build covers the Profile 3 scope of the paper: the Observability Plane plus Tier 3.

## 2. Results on the Kali box

All on 4 October 2026, ARTO commit `b57be5e`, Vestigia image `b57be5e-p1`.

| Test | Result | Evidence |
|---|---|---|
| Foundation test (`smoke_foundation.py`) | 23 of 23 | Output seen |
| Tool path test (`e2e_gateway.py`) | 51 of 51, rerun after Stage 3 | Output seen (final line) |
| Model path test (`e2e_models.py`), mock model | 20 of 20 | Output seen |
| Agent isolation probe | 9 of 9: an agent reaches only the gateway and the provisioner | Output seen |
| Fail-closed test, all four dependencies | Passed | Reported by Christophe |
| Ledger verifier | Chain verifies: 505 entries, 504 hashes recomputed, salted | Output seen |
| Ledger curve, five minutes from empty | 27 ms rising to 208 ms at 4,000 to 5,000 entries. 2,499 writes, none refused | Output seen. Saved in `results/` on the Kali box |
| Stage 3 image build | All six images built. About 460 seconds | Output seen |

Head hash of the ledger at the time of the verifier run: `56798917667f9ff118370b0a9a2d16b1c0558bc33a48ac7a2791b44b0c590a5a`.

## 3. Stage 4: telemetry, as built

| Item | Location | Role |
|---|---|---|
| Collector | `otel-collector` service, `observability/otel-collector.yaml` | Receives OTLP over HTTP on the control network. Writes traces and metrics as JSON lines to the `telemetry-data` volume. Exposes metrics for scraping on port 8889 |
| Gateway instrumentation | `gateway/telemetry.py` | OpenTelemetry SDK 1.45.0. One trace per request, one child span per stage, a decision counter, and a duration histogram |
| Summary tool | `tools/telemetry_summary.py`, `telemetry-show` service | Reads the collector's files with no network. `--check` verifies telemetry is complete |

Design points:

- **Telemetry fails open, audit fails closed.** Telemetry is exported in the background. A collector that is down never delays or blocks a request. The ledger remains the record of decisions.
- **Trace and ledger share an identifier.** The gateway's `trace_id` is the OpenTelemetry trace identifier, and it is written to the ledger and returned to the caller.
- **Sampling is done in the collector.** Every denied, referred, or failed request is kept, and one in ten allowed requests. Metrics count every request. This is the "exception-only" mode the paper describes for Profile 3.
- **Content is kept out.** Outcome, reasons, agent, role, tool or model name, and timings are sent. Tool parameters and prompt content are not.
- **A policy denial is an outcome, not an error.** Only dependency failures mark a span as an error.
- The provisioner, Tessera, Vestigia, and OPA are not instrumented. OPA's decision log goes to its container log.

Verified in the development container (plain processes, real collector binary 0.162.0, not under Docker):

| Check | Result |
|---|---|
| Collector configuration | Valid, checked by the collector's own `validate` command |
| Both acceptance tests with telemetry on | 51 of 51 and 20 of 20 |
| What the collector recorded | 37 requests counted. 27 traces kept: all 21 denials, all 5 referrals, 1 of 11 allowed |
| Trace to ledger match | A trace identifier from the collector was found in the ledger |
| Collector stopped | Both acceptance tests still pass, with no error lines in the gateway log |
| Summary tool `--check` | Passed, including the check that no parameter or prompt attribute is present |

Not verified: the collector container, its volume permissions (it runs as root with all capabilities dropped, since the image has no shell to prepare the volume), and the `telemetry-show` service. The Kali run tests these.

Differences from Chapter 6 to record:

- The collector is a single service on the control network, not a sidecar DaemonSet.
- Exporters are files and a Prometheus scrape endpoint. Langfuse, Jaeger, Splunk, and the custom Vestigia exporter in the Chapter 6 sample are not used.
- Audit goes from the gateway straight to Vestigia, synchronously. It does not pass through the collector.
- Table 16's data flows ("Tessera → OTel", "OPA → OTel", "LiteLLM → Prometheus") do not exist in this build. The gateway is the one source of telemetry.

## 4. Not yet done

| No. | Item | Command or note |
|---|---|---|
| 1 | Run Stage 4 on the Kali box | Build, `up -d --wait`, both acceptance tests, then `telemetry-show` with `--check` |
| 2 | Result of the real provider call | In progress on the Kali box |
| 3 | Result of the gateway latency run | In progress on the Kali box |
| 4 | Saved output of the fail-closed and end-to-end runs, as evidence | `... 2>&1 | tee results/<name>.txt` |
| 5 | Full ledger curve through rotation, on the Kali box | Optional |

## 5. Decisions in force

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

## 6. Findings to report upstream (Miracle Owolabi)

1. Vestigia's API rate limit is fixed in source at 10 requests a second.
2. Vestigia's integrity check reports the ledger invalid after rotation, although the chain is continuous.
3. The rotation threshold is fixed at 10,000 entries in the API server. `VESTIGIA_MAX_ENTRIES` is not read.
4. Each write rewrites the whole ledger file, so write time grows linearly with ledger size.
5. Tessera's revoke, validate, and agent-list endpoints accept unauthenticated calls in strict mode.
6. Tessera's registration and token issuance are unauthenticated unless `TESSERA_REQUIRE_REGISTRATION_AUTH` is set.

## 7. Open work, in suggested order

| No. | Item |
|---|---|
| 1 | Section 4, items 1 to 3 |
| 2 | Replace the example registry with real purposes, tools, models, and limits |
| 3 | Chapter 6 and Chapter 7 corrections (decision record section 12, Stage 2 notes section 5.3, Stage 3 notes section 8, and section 3 of this page) |
| 4 | Restrict the model adapter's egress to the provider |
| 5 | Dependency audit of ARTO's pinned packages, the adapter's set, and the services' set |
| 6 | Key hygiene: the Anthropic key pasted into the working session on 4 October should be revoked and replaced |

## 8. Detailed notes

| Document | Covers |
|---|---|
| `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md` | Design decisions, ARTO source findings, Chapter 6 corrections |
| `claude/POLICY_GATEWAY_STAGE1_BUILD.md` | Foundation services |
| `claude/POLICY_GATEWAY_STAGE2_BUILD.md` | Tool path, provisioner, rate limit patch, ledger write cost |
| `claude/POLICY_GATEWAY_LATENCY_HARNESS.md` | Harness method, development container figures, rotation finding |
| `claude/POLICY_GATEWAY_STAGE3_BUILD.md` | Model path, adapter, verifier, Kali ledger curve |

Some status lines in the detailed notes predate the Kali runs. This page is the current status.
