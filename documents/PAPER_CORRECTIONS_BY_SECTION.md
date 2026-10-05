# Paper corrections by section

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Paper | NUS_CH6_Operationalisation_302-Capstone_Runtime_AI_Governance.md (Abstract, Chapter 1, Chapter 6, Chapter 7) |
| Basis | The Profile 3 build in `~/Projects/governance-tier3`, ARTO commit `b57be5e`, and the Kali box results of 4 October 2026 |
| Sources | Decision record, Stage 1 to 3 build notes, latency harness notes, build status page |
| Use | Work through the paper top to bottom. Each row gives the location, what it says now, and what to change |

## How to read this list

| Type | Meaning |
|---|---|
| Fact | The paper states something the build or the ARTO source contradicts. Must change |
| Scope | True of the framework, but not built or measured in Profile 3. Say so, or move it |
| Code | A sample that does not run as written |
| Add | Something the build produced that the paper does not yet mention |
| Edit | Wording, numbering, or leftover text |

Chapters 2 to 5 were not available to review. Check them for the same terms, especially "LiteLLM gateway", "Redis", "PostgreSQL", and "sub-millisecond".

## 1. Abstract

| No. | Type | Now | Change |
|---|---|---|---|
| A1 | Scope | "kernel-level telemetry mechanisms" and "eBPF telemetry" (keywords) | The build has no eBPF. State it as part of the framework design that Profile 3 does not implement, or remove |
| A2 | Scope | "semantic validation controls designed to detect adversarial behaviour" | Tier 2 was not built or tested. Attribute its figures to Owolabi's work, not to this evaluation |
| A3 | Fact | "evaluated using a dedicated laboratory environment described in Chapter 6" | The evaluation covers Profile 3 only: the Observability Plane and Tier 3. Say so |
| A4 | Fact | "predominantly open-source technologies" | Now exact: every component is open source, with one documented patch to ARTO. Worth stating precisely |

## 2. Chapter 1

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 1.1 | 1.7, Tier 3 | Scope | "policy engine access rules compiled into inline enforcement plugins" and "OpenAPI contract compliance" | The build queries OPA over HTTP from a policy gateway. There is no compiled plugin and no OpenAPI contract check. Reword to "a policy gateway that consults a policy engine on every call" |
| 1.2 | 1.7, Tier 3 | Add | Two outcomes implied: allow or deny | Three outcomes: deny, refer to a human (hold or flag), allow. Declared intent is a Tier 3 input |
| 1.3 | 1.7, Observability Plane | Fact | "unified sidecar signal collection layer providing runtime kernel-level telemetry capture" | In Profile 3 it is one collector service receiving gateway telemetry, plus the audit ledger. No sidecar and no kernel-level capture |
| 1.4 | 1.8.1, Profile 3 | Fact | "reactive (incidents are discovered forensically, not in real time) and permissive (whitelisted tools are trusted to be used correctly)" | With identity, declared intent, parameter thresholds, and human hold, Profile 3 is neither in that sense. Suggested: "deterministic: it checks who is calling, for what declared purpose, and within what limits, but does not judge meaning" |
| 1.5 | 1.8.1, Profile 3 | Fact | "Observability Plane (in lightweight, asynchronous mode)" | Two parts behave differently: audit is synchronous and fails closed, telemetry is asynchronous and fails open. Say both |
| 1.6 | 1.8.1, Gate 1 | Add | "At least one engineer dedicated to gateway maintenance" | Add the reviewer: held calls need a named human. An unstaffed hold queue is the governance theatre 1.8.2 warns against |
| 1.7 | 1.8.1, Profile 3 uses | Fact | "Single-provider deployments", "no multi-turn reasoning" | Consistent with the build (one provider, no streaming, per-call decisions). Keep, and add that external providers are the expected case for limited budgets |
| 1.8 | 1.9 | Fact | "Chapter 7 concludes with experimental evidence ... in production environments" | Chapter 7 itself says laboratory validation, not production. Align |

## 3. Chapter 6

### 3.1 Section 6.1: executive summary and security properties

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 6.1 | 6.1.1, component list | Fact | "Observability Plane: OpenTelemetry + Vestigia hash-chaining + PostgreSQL audit logs" | "OpenTelemetry Collector + Vestigia hash-chained file ledger". No PostgreSQL |
| 6.2 | 6.1.1, component list | Fact | "Tier 3: Tessera IAM + OPA policies + LiteLLM gates" | "Tessera + OPA + a policy gateway, a provisioning service, and a model adapter built on the LiteLLM library" |
| 6.3 | 6.1.1 | Add | Nothing on provenance | State that Tessera and Vestigia come from github.com/ArksherX/ARTO, Apache-2.0, pinned at commit `b57be5e`, with one build-time patch |
| 6.4 | 6.1.1 | Edit | "with only limited depending on funding and technical skill proficiency. The solution makes AI runtime governance." | Two broken sentences. Rewrite |
| 6.5 | 6.1.1, item 2 | Fact | "annual average infrastructure cost in the region of 1'500 to 15'000 SGD for about 70% of Singapore's organisations" | Unsupported by the build. Either source it or remove the 70 per cent claim. Tables 21 to 23 are in USD |
| 6.6 | 6.1.1, item 5 | Scope | "Achieving compliance (EU AI Act, NIST RMF, ISO 42001 ready)" | Not tested. Soften to "supports evidence gathering for" |
| 6.7 | 6.1.2 | Edit | Subsections numbered 6.1.1.1 and 6.1.1.2 | Renumber as 6.1.2.1 and 6.1.2.2 |
| 6.8 | Table 13, Identity | Fact | "Each request signed with agent's Ed25519 key" | "HS512 access token issued by Tessera, bound to the agent's key by an ES256 DPoP proof on every request". Ed25519 is used only for optional action signatures, which are off |
| 6.9 | Table 13, Integrity | Fact | "SHA-256 chain" | "HMAC-SHA256 chain, keyed with the ledger salt" |
| 6.10 | Table 13, Non-repudiation | Fact | "Agent cannot deny it made the request" | Qualify. The DPoP proof covers the method and URL, not the request body, and the token is symmetric, so Tessera could mint one. What holds: the request reached the gateway with a proof only the agent's key could make |
| 6.11 | Table 13, Immutability | Fact | "Hash-chained PostgreSQL logs" | "Hash-chained JSON file ledger". Tampering is detectable, not prevented |
| 6.12 | Table 13, Revocation | Fact | "Redis-backed instant propagation, <0.5ms" | "File-backed revocation list with a Valkey cache. Effective on the next request". The 0.5 ms figure was not measured. Remove it |
| 6.13 | Table 13, Auditability | Fact | "OpenTelemetry + SIEM export" | "Ledger and OpenTelemetry share a trace identifier". No SIEM export in Profile 3 |
| 6.14 | Table 14, Prompt injection and Semantic subversion | Scope | Tier 2 rows, "73% catch" | Out of Profile 3 scope. Mark as Tier 2, cite Owolabi, and reconcile 73 per cent with the 76.9 per cent in Chapter 7 |
| 6.15 | Table 14, Model drift | Scope | Tier 1 row | Out of Profile 3 scope |
| 6.16 | Table 14, Unauthorised tool call | Fact | "Residual risk: none (deny by default)" | "The whitelist governs which tool. Parameter thresholds and declared intent limit how it is used, but do not remove misuse of a permitted tool" |
| 6.17 | Table 14, Credential forgery | Fact | "Requires key compromise" | Name both keys: the agent's private key, or Tessera's signing secret, which alone would let an attacker mint tokens |
| 6.18 | Table 14, Log tampering | Fact | "None (hash chain detects tampering)" | Three limits: removal of the newest entries is undetectable without a recorded head hash, anyone holding the salt can forge entries, and Vestigia's own check reports a false alarm after rotation |
| 6.19 | Table 14, Revocation bypass | Fact | "Requires Redis compromise" | The file list is authoritative. The real exposure is that Tessera's revoke endpoint is unauthenticated, so it must be unreachable by agents |
| 6.20 | Table 14 | Add | No row for prompt content | Add: "Prompt content leaves the organisation on every external model call. Tier 3 does not read prompts. Mitigation: Tier 2" |

### 3.2 Section 6.2.1: Figure 4

| No. | Type | Now | Change |
|---|---|---|---|
| 6.21 | Fact | "LiteLLM Gateway (Tier 1 config)" as the central box | The policy gateway is the central box. The model adapter sits behind it. LiteLLM is a library inside the adapter and belongs to no tier |
| 6.22 | Fact | "Agent-level budget enforcement", "Model selection by agent role", "Cost tracking" under LiteLLM | These are done by the gateway and OPA from the registry, with counters in Valkey |
| 6.23 | Fact | One OPA box before the model call, "<0.1ms latency" | Two decision points on two paths: model path and tool path. Measured policy decision: 1.1 ms median |
| 6.24 | Fact | "Artonexa Vestigia (Tier 1: Forensic Audit)" | Vestigia is part of the Observability Plane |
| 6.25 | Scope | VerityFlux box in the request path | Not in Profile 3. Show it as a later tier, greyed or separate |
| 6.26 | Scope | Paperclip layers, agent roles, dashboard | Not in the build. See 6.38 |
| 6.27 | Fact | Observability box lists Prometheus, Langfuse, PostgreSQL, SIEM | Profile 3 has the collector writing files, and a scrape endpoint for later |
| 6.28 | Add | No provisioning component | Add the provisioner as the only holder of the Tessera admin key |
| 6.29 | Add | No networks shown | Show the boundaries: agents, control, tools, models, egress. Only the model adapter has a route out |
| 6.30 | Fact | "allow/deny" and "allow/escalate/block" | Tier 3 itself returns deny, refer, or allow |

### 3.3 Section 6.2.2: technology stack

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 6.31 | 6.2.2.1, Table 15 | Scope | Paperclip as the orchestration layer | Not built or licence-checked. Present as an intended integration. The earlier sandbox used a stand-in, not the framework |
| 6.32 | Table 16 | Fact | Flows: "Tessera → OTel → Prometheus", "OPA → OTel → PostgreSQL", "LiteLLM → Prometheus", "PostgreSQL → SIEM" | In the build the gateway is the one source of telemetry, and audit goes from the gateway straight to Vestigia. Redraw the table from the build |
| 6.33 | Table 16, Tool execution | Fact | "Tools → Vestigia → PostgreSQL" | "Gateway → Vestigia (file ledger)", synchronously, before and after the tool |
| 6.34 | Figure 5 | Fact | Sidecar DaemonSet, Langfuse, Vestigia downstream of the collector, SIEM export | Collector as one service. Traces and metrics to files. Vestigia is not fed by the collector |
| 6.35 | Collector sample | Code | `otlp_custom` exporter, `resource_detection`, `jaeger` exporter, `attributes` actions without `action`, Prometheus on 8888 | Replace with `observability/otel-collector.yaml` from the build, which the collector's own `validate` command accepts |
| 6.36 | Collector sample | Fact | Tail sampling on error status only | The build samples on outcome: every deny, refer, and failure is kept, one in ten allows. A policy denial is an outcome, not an error |
| 6.37 | 6.2.2.2 | Add | Nothing on failure behaviour | State the rule: telemetry fails open, audit fails closed |
| 6.38 | 6.2.2.3, opening | Fact | "combines identity verification, policy enforcement, and gateway controls through Artonexa Tessera, OPA, and LiteLLM" | Name the policy gateway and the provisioner as own code, and LiteLLM as the adapter's library |
| 6.39 | Table 17, Budget enforcement | Fact | "LiteLLM virtual keys" | "Registry limits decided by OPA, counters in Valkey" |
| 6.40 | Table 17, Request cryptography | Fact | "HMAC + timestamp validation" | Match Table 13: HS512 token plus ES256 DPoP proof with replay cache |
| 6.41 | Table 17, Revocation | Fact | "Redis-backed revocation cache, instant propagation" | As 6.12 |
| 6.42 | Table 17 | Add | No row for intent or referral | Add: declared intent (purpose code and task), referral with hold or flag, tool destination from the registry |
| 6.43 | Tessera sample | Code | `from tessera import Agent, Credential, RevocationManager` | Does not match how the build uses Tessera, which is over HTTP: register, request token, validate, revoke. Replace with the provisioner flow, or verify the sample against the source |
| 6.44 | Tier 3 Rego | Code | Missing parentheses, `time.now_ns() \| timeofday`, undefined `now`, `deny["..."] if`, budget summed with no time window | Replace with `policies/tool_egress.rego` and `policies/model_egress.rego`: 29 policy tests pass, `opa check --strict` clean |
| 6.45 | 6.2.2.3 | Add | One token per agent implied | A Tessera token covers one tool. Agents hold one token per tool, plus one for the model path |
| 6.46 | 6.2.2.4, Tier 2 | Scope | Whole section, including per-detector latencies | Not built or measured. Label as design, attribute figures to their source |
| 6.47 | 6.2.2.4 | Edit | "Table Y" | Should read Table 18 |
| 6.48 | Tier 2 Rego | Code | Rules without `if`, no `import rego.v1` | Will not compile under OPA 1.x. Rewrite or mark as pseudocode |
| 6.49 | 6.2.2.5, Tier 1 | Fact | "sha256: Cryptographic hash of model weights" for `claude-3-5-haiku` and `perplexity-sonar` | The weights of a hosted provider's model cannot be hashed by the customer. For hosted models, pin the provider and model identifier and verify behaviour against a baseline. Hashing applies to self-hosted weights |
| 6.50 | 6.2.2.5 | Edit | "Insert existing models_registry.yaml example here." | Remove |

### 3.4 Sections 6.3 to 6.6

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 6.51 | 6.3.1 | Fact | Repository tree by tier, with Kubernetes and Terraform | Replace with the build's layout: `gateway/`, `provisioner/`, `model-adapter/`, `policies/`, `observability/`, `docker/`, `scripts/`, `tests/`, `tools/`, `vendor/ARTO/`. Docker Compose only |
| 6.52 | 6.4 and 6.5 | Scope | Paperclip integration described as working | Not built. Reframe as design, or cut to a short forward-looking section |
| 6.53 | 6.5.1 | Edit | "Budget: $1,000 tokens/month" | Mixes dollars and tokens. Choose one unit |
| 6.54 | 6.5.2 | Edit | Budget goes $500, then $400, then $300, then "$50 of $200" | The arithmetic does not reconcile. Rework the example |
| 6.55 | 6.5.2, step 4 | Fact | VerityFlux check after the OPA allow | Not in Profile 3 |
| 6.56 | 6.5.2, step 7 | Fact | "Chain validity: All hashes verify" by the compliance officer | Add how: the independent verifier, since Vestigia's own check fails after rotation |
| 6.57 | 6.5, delegation | Scope | Delegation chains to depth 3 | Depth 5 is confirmed in Tessera's source. The build does not exercise delegation. Say so |
| 6.58 | 6.6, step 1 to 3 | Fact | Denial "before reaching LLM" of a tool the agent has already chosen | The denial happens at the policy gateway on the tool path, after the model has responded |
| 6.59 | 6.6, step 2 | Fact | "Signature valid? (Ed25519)", "Revoked? (not in Redis cache)" | As 6.8 and 6.12 |
| 6.60 | 6.6, step 3 | Fact | "t=<0.1ms", "OPA evaluates GOPAL policy" | Measured 1.1 ms median. The policies are the build's own. GOPAL was not used |
| 6.61 | 6.6, step 4 | Fact | "Skipped (blocked at Tier 3 before reaching LLM)" | "Tier 2 is not deployed in Profile 3" |
| 6.62 | 6.6, step 5 | Fact | "Persist to PostgreSQL", "t=<5ms after denial" | File ledger. Measured audit write: 57 ms at about 900 entries, rising with ledger size, and written before the response, not after |
| 6.63 | 6.6, step 6 | Fact | "Cost: $0 (denied before LLM call)", Prometheus, Langfuse, Grafana | The model call that produced the tool choice was already paid for. Telemetry is the collector's files |
| 6.64 | 6.6, step 7 and 8 | Fact | SIEM export, "PostgreSQL contains full chain" | No SIEM in Profile 3. File ledger |
| 6.65 | 6.6, audit entry | Add | No intent or reasons | The build's entry carries intent, reasons, role, token identifier, and the trace identifier |

### 3.5 Section 6.7: roadmap and cost

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 6.66 | Table 20, Week 1 | Fact | "Deploy LiteLLM gateway", "baseline Rego policies (GOPAL)" | Week 1 as built: Tessera, Vestigia, OPA, Valkey, then the policy gateway and provisioner |
| 6.67 | Table 20, Week 2 | Fact | Prometheus, Grafana, Langfuse, PostgreSQL | Profile 3: the OpenTelemetry Collector only |
| 6.68 | Table 20 | Edit | "Verify end-to-end trace collection." has no checkbox | Add it |
| 6.69 | 6.7.1 | Add | No timeline evidence | The build went from empty folder to four passing stages in one working session. Report that as the observed effort, with its limits |
| 6.70 | Table 21 | Fact | PostgreSQL 50 USD, Redis 25 USD a month | Profile 3 needs no PostgreSQL. Redis becomes Valkey. Recompute |
| 6.71 | 6.7.2 | Edit | "Refer to Appendix A" | No Appendix A in the draft. Add it or remove the reference |
| 6.72 | 6.7.2 | Add | Infrastructure cost only | Add the provider's usage cost, since Profile 3 assumes an external model provider |

## 4. Chapter 7

### 4.1 Setup and scope

| No. | Location | Type | Now | Change |
|---|---|---|---|---|
| 7.1 | 7.1.1 | Fact | "specifications to be added", "Kubernetes (minikube or local k3s)" | Kali Linux, 16 CPUs, 15 GB memory, Docker 29.7.2, Docker Compose. No Kubernetes |
| 7.2 | 7.1.1 | Fact | "All three profiles (Profile 1, 2, 3) run on the same host" | Only Profile 3 was built. Rescope the chapter, or mark Profiles 1 and 2 as future work |
| 7.3 | 7.1.1, versions | Fact | "latest stable", "PostgreSQL (14.x)", "Redis" | Pinned: OPA 1.19.1, Valkey 9.0.6, Collector 0.162.0, LiteLLM library 1.104.0, ARTO `b57be5e` with patch p1. No PostgreSQL |
| 7.4 | 7.1.2 | Fact | "1 req/sec to 500 req/sec", open-loop rates | Closed-loop load with 1 and 4 workers. The stack sustained 1.3 to 16.5 requests a second depending on outcome and ledger size |
| 7.5 | 7.1.2 | Scope | Adversarial payload injection, multi-turn jailbreaks | Tier 2 material. Not run |

### 4.2 Research questions and hypotheses, restated with results

| No. | Paper hypothesis | Result on the Kali box | Suggested restatement |
|---|---|---|---|
| 7.6 | "Tier 3 latency p99 stays below 0.1ms even at 500 req/sec" | Not met. Policy decision 1.1 ms median. End to end 60 to 371 ms median at one worker | "The deterministic checks (identity, policy, state) add about 4 to 5 ms at the median. End-to-end time is dominated by the audit write" |
| 7.7 | "Credential verification via Tessera adds <0.01ms" | Not met. 2.9 ms median, one network hop | State the measured figure |
| 7.8 | "OPA policy evaluation time increases sub-linearly with policy complexity" | Not tested. One policy set | Drop, or mark as future work |
| 7.9 | "100% event completeness", "100% hash-chain validity" | Supported within one ledger file: every decision was written before the action, and the chain verified at 505 and 5,044 entries | Add the rotation limit: after 10,000 entries Vestigia's own check fails, and the independent verifier is needed |
| 7.10 | "Profile 3 (60-second batching): median 35-40 second lag" | Not applicable. The build writes audit synchronously | "Audit lag is zero by design: the decision is recorded before the action. The cost is latency" |
| 7.11 | "if PostgreSQL is slow, audit trail still completes ... no data loss" | Not applicable. If the ledger is unavailable, the request is denied | "No decision proceeds unrecorded" |
| 7.12 | Tier 2 detection, 74 to 78 per cent | Not tested | Out of scope for this evaluation |
| 7.13 | "Profile 3 footprint <1 GB RAM, <500 MB storage per 24 hours" | Not measured. Container memory limits total about 2.9 GB | Measure with `docker stats`, or state the limits |
| 7.14 | "Policy update propagation <30 seconds" | Not met as built. A policy change needs an OPA restart | State it, or add OPA's watch option and measure |
| 7.15 | "Audit log query latency <30 seconds for Profile 3" | Not measured. An integrity check of 5,044 entries took 0.2 s | Use the measured figure |
| 7.16 | "All failures fail closed" | Supported. OPA, Tessera, Vestigia, and Valkey were each stopped: every call denied, no tool reached, service resumed | Keep, with the evidence |
| 7.17 | "Redis failure ... does not block enforcement" | Opposite by design. Without the store the gateway denies | "The gateway denies while the state store is down" |
| 7.18 | "PostgreSQL failure does not block enforcement; audit logs buffer in-memory" | Opposite by design | As 7.11 |
| 7.19 | "Recovery is automatic and completes within 30 seconds" | Supported for all four dependencies, and after a host reboot | Keep. Recovery time was not measured precisely |
| 7.20 | "No cascading failures" | Partly. Telemetry outage does not affect requests (shown in the development container). The gateway does wait for the model adapter at start | State both |
| 7.21 | "Tessera depth-5 parameter proves operationally sound" | Confirmed in source only. Delegation not exercised | Say so |
| 7.22 | "Profile 3 resource consumption matches ... sub-USD 200/month" | Not measured | Recompute from Table 21 without PostgreSQL |

### 4.3 Results to add

| No. | Add |
|---|---|
| 7.23 | **Per-stage latency table** (one worker): identity 2.9 ms, policy 1.1 ms, store under 1 ms, tool 1.1 ms, mock model 4.2 ms, audit 57 to 180 ms per write as the ledger grew from 600 to 4,500 entries |
| 7.24 | **Audit share:** for an allowed tool call, 190 of 199 ms at the median is two ledger writes |
| 7.25 | **Ledger curve:** 27 ms below 1,000 entries, rising to 208 ms at 4,000 to 5,000, about 0.045 ms per entry. Every row must quote the ledger size |
| 7.26 | **Concurrency:** four workers gave lower throughput and about ten times the latency, since ledger writes are serialised |
| 7.27 | **Functional results:** 23, 51, and 20 acceptance checks, 10 isolation checks, 9 fail-closed checks, 29 policy tests |
| 7.28 | **Network isolation:** an agent reaches only the gateway and the provisioner |
| 7.29 | **Model path:** role, token ceiling, rate, and budget enforced. Offered tools filtered. Prompt text absent from the ledger |
| 7.30 | **Telemetry:** every denial and referral traced, one in ten allows, trace identifier shared with the ledger |
| 7.31 | **Restart persistence:** all services healthy and the chain valid after a host reboot (light test, 35 entries) |
| 7.32 | **Findings in the open-source components**, six in all: fixed rate limit, false integrity alarm after rotation, fixed rotation threshold, whole-file rewrite, unauthenticated Tessera endpoints, open registration by default |
| 7.33 | **Method statement:** monotonic clock, warm-up discarded, closed-loop load, ledger size recorded per run, swap growth flagged |

### 4.4 Limitations to add to 7.8

| No. | Add |
|---|---|
| 7.34 | One patch to upstream code, applied at image build |
| 7.35 | Audit throughput bounded by the file ledger. A higher-volume profile needs a different store or a configurable rotation |
| 7.36 | Integrity across rotation shown in a development container, not yet on the Kali box |
| 7.37 | The independent verifier shares the ledger salt, so it is independent in code and process, not in key |
| 7.38 | Reviewer and operator access are shared keys. Reviewer identity is recorded as given |
| 7.39 | External tools are simulated. The model adapter's egress is not restricted to the provider |
| 7.40 | One real provider call was planned. Record its result, or state that provider calls were tested with a mock |
| 7.41 | The registry is example data. Limits and budgets are illustrative |
| 7.42 | Timescale: the paper says "6-week validation window". The build and tests took place on over 3 days. Correct it |

## 5. Terms to change throughout

| Now | Use |
|---|---|
| LiteLLM gateway, API gateway (for the enforcement point) | Policy gateway |
| Redis | Valkey, or "key-value store" |
| PostgreSQL audit logs | Vestigia file ledger |
| Artonexa Tessera, Vestigia, VerityFlux | Keep the names, and cite the ARTO repository, licence, and commit once |
| allow/deny | deny, refer, allow |
| sub-millisecond, <0.1ms | The measured figures |
| catch rate 73% and 76.9% | One figure, with its source |

## 6. Suggested order of work

1. Section 5 of this list first: a global pass on terms.
2. Chapter 7, since it now has real results and most of its hypotheses change.
3. Chapter 6 tables 13, 14, 16, 17, and Figures 4 and 5.
4. Chapter 6 code samples, replaced from the build.
5. Abstract and Chapter 1 last, so they match what Chapters 6 and 7 end up saying.
