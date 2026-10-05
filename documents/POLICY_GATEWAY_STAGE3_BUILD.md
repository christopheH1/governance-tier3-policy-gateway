# Policy gateway: Stage 3 build notes (model path, ledger verifier)

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Status | Build files delivered as `governance-tier3-stage3.tar.gz`. Not yet run under Docker on the Kali box |
| Build location | `~/Projects/governance-tier3` |
| Companions | `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md`, `claude/POLICY_GATEWAY_STAGE2_BUILD.md`, `claude/POLICY_GATEWAY_LATENCY_HARNESS.md` |
| Supersedes | Section 5 (decision needed) and section 6 (next steps) of the latency harness notes |

## 1. Decisions taken

| Item | Decision | By |
|---|---|---|
| First external provider | Anthropic | Christophe |
| Integrity checking across rotation | Independent verifier in own code | Christophe |
| Next build step | Stage 3 model path | Christophe |
| Provider adapter | Own thin adapter on the LiteLLM library, not the LiteLLM proxy | Chosen in the build for licence reasons. See section 3 |
| Provider model | `anthropic/claude-haiku-4-5-20251001`, registered as `claude-haiku` | Default, open to change |
| Egress | A non-internal `egress` network attached to the model adapter only | Default, open to change |
| Model path limits | Per-model token ceiling and requests a minute, per-role daily token budget, all in the registry | Default values are examples |
| Prompt content in the ledger | Not stored. Size and SHA-256 fingerprint only | Default, open to change |

## 2. Kali box results recorded since the last notes

Ledger curve, run under Docker on 4 October 2026, limited by Christophe to five minutes, from an empty ledger. Saved on the Kali box as `results/latency-ledger-curve-20261004-100751.*`.

| Ledger entries | Writes | Median write | p95 | Writes a second |
|---|---|---|---|---|
| 0 to 999 | 496 | 27.2 ms | 46.7 ms | 36.5 |
| 1,000 to 1,999 | 497 | 72.0 ms | 98.6 ms | 13.9 |
| 2,000 to 2,999 | 495 | 115.8 ms | 147.6 ms | 8.6 |
| 3,000 to 3,999 | 495 | 162.5 ms | 202.2 ms | 6.1 |
| 4,000 to 4,999 | 494 | 208.4 ms | 247.7 ms | 4.7 |
| 5,000 to 5,999 | 22 | 223.1 ms | 254.1 ms | 4.3 |

- 2,499 writes accepted, none refused. The rate limit patch holds under Docker.
- Growth is linear at about 0.045 ms per ledger entry. Extrapolated to the 10,000-entry rotation point, a write would take about 450 ms.
- Vestigia's integrity check afterwards: valid, 5,044 entries, 0.2 seconds.
- Rotation was not reached in this run, so the rotation finding rests on the development container run (latency harness notes, section 4.4).
- `scripts/reset-ledger.sh` worked under Docker, before and after the run.

Not yet seen from the Kali box: the gateway latency run, and the end-to-end regression result after the gateway was instrumented.

## 3. Model adapter: why not the LiteLLM proxy

Installing `litellm[proxy]` brings in `litellm-enterprise` as a dependency (117 packages in all). That package is under LiteLLM's separate enterprise licence, so an image built that way would contain non-MIT code even if no enterprise feature were used. Installing the `litellm` library alone brings 63 packages and no enterprise package.

The build therefore uses the library inside a small own-code adapter (`model-adapter/app.py`, about 75 lines). It also has a smaller surface: no admin interface, no login, no database.

The adapter:

- accepts calls only with `MODEL_ADAPTER_KEY`, which only the policy gateway holds
- maps a registered model name to a provider model through `model-adapter/routes.json`
- holds the provider key (`ANTHROPIC_API_KEY`) and is the only container with a route out
- makes no decisions, and returns only the error type on a provider failure, never the provider's error text
- offers a `mock-model` route that returns a canned reply without calling any provider, for tests at no cost

Versions: `litellm==1.104.0`, fully pinned in `docker/requirements-adapter.txt`.

## 4. Model path as built

```
  Agent --- token (model_access) + DPoP proof + intent + model + messages --->  Policy gateway
                                                                                     |
        1. Tessera validates the credential (proof bound to the model path URL)      |
        2. Valkey: count this request, read tokens used today                        |
        3. OPA: deny or allow, plus token ceiling and the tools that may be offered  |
        4. Vestigia: decision recorded before the call                               |
        5. Model adapter ---> provider                                               |
        6. Vestigia: completion recorded with token usage, counters updated         <+
```

Endpoint: `POST /v1/models/chat`. Token: the provisioner issues a token for the capability `model_access` to any agent whose role is permitted at least one model. A tool token cannot open the model path, and a model token cannot call a tool.

Policy package `ai_governance.tier3.model_egress`:

| Outcome | Reasons |
|---|---|
| Deny | `identity_not_verified`, `intent_not_declared`, `task_not_declared`, `intent_not_recognised`, `model_not_registered`, `model_not_permitted`, `max_tokens_exceeded`, `rate_limit_exceeded`, `token_budget_exceeded`, `no_matching_rule` |
| Allow | `all_constraints_satisfied` |

Facts returned with the decision: `max_tokens` (the ceiling applied when the request names none), `location`, and `permitted_tools`.

Tools offered to the model: the gateway removes any offered tool that is not in the agent's role and covered by the declared purpose. This is the model-path control identified in the decision record: the model is never shown a tool the agent could not use. Tool calls the model proposes are recorded by name in the ledger as a detective signal. They are still decided on the tool path.

Ledger events: `MODEL_DECISION` (allowed or denied, with model, role, intent, message count, prompt size, prompt fingerprint, tools offered and removed) and `MODEL_COMPLETED` (token usage, finish reason, proposed tool calls, response fingerprint).

Fail closed: adapter not configured, adapter unreachable, or provider failure all end in deny. A decision that cannot be recorded is a deny.

Stage timing: the `model` stage is reported in the `Server-Timing` header, and the latency harness has a `model_allow` scenario against the mock model.

## 5. Independent ledger verifier

`tools/verify_ledger.py`, standard library only, about 130 lines. Run with `docker compose --profile test run --rm ledger-verify`, which mounts the ledger volume read-only and has no network.

It reads the archived ledgers and the current ledger in order and checks:

1. the first ledger begins with the genesis entry
2. every later ledger begins with a rotation entry carrying the last hash of the ledger before it
3. every entry's `previous_hash` equals the hash of the entry before
4. every entry's hash, recomputed from its contents with the ledger salt, equals the stored hash

Tested on the rotated ledger from the development container run (10,000 archived entries plus 659 current):

| Case | Result |
|---|---|
| Untouched | Chain verifies across both files. 10,657 hashes recomputed |
| One field edited in the archive | `HASH_MISMATCH` at that entry |
| One entry deleted from the current ledger | `BROKEN_CHAIN` at that position |
| Archive removed | `MISSING_ARCHIVE` |
| Wrong salt | Every hash fails |

This confirms that Vestigia's chain is continuous across rotation, and that its built-in check reports a false alarm there.

Limits of the verifier, to state in the paper:

- It cannot notice that the newest entries were removed, unless the expected head hash is known. It prints the head hash so it can be recorded outside the machine.
- Genesis and rotation entries are markers. Their own contents are not covered by a hash, in Vestigia's design.
- It needs the ledger salt, so whoever runs it can also forge entries. Independence here means independent code and process, not an independent key.

## 6. Verification

In the development container, services as plain processes on Python 3.11. Not under Docker.

| Test | Result |
|---|---|
| Policy tests | 29 of 29, `opa check --strict` clean |
| Model path acceptance test (`e2e_models.py`), mock model | 20 of 20 |
| Tool path acceptance test (`e2e_gateway.py`), regression | 51 of 51 |
| Foundation test, regression | 23 of 23 |
| Latency harness, `model_allow` scenario | Ran. Gateway overhead about 64 ms median, of which about 51 ms is two ledger writes |
| Adapter route to Anthropic | Reached the provider and was refused with a dummy key, which shows the route works. No real completion was made |
| Ledger verifier | Section 5 |

Not verified: the Stage 3 compose file, the adapter image, the `models` and `egress` networks, the `ledger-verify` service, and a real provider call. The Kali run tests these.

## 7. Limits to state in the paper

- Prompt content leaves the organisation on every external model call. Tier 3 does not read prompts.
- The model adapter can reach any internet address. Limiting it to the provider needs a host firewall rule or a forwarding proxy.
- No streaming on the model path.
- Usage counters are kept in Valkey. Requests are counted per minute window and tokens per UTC day.
- Limits and budgets in the registry are example values.
- The gateway waits for the adapter to be healthy at start, so an adapter that fails to start also holds back the tool path.

## 8. Consequences for Chapter 6

- Figure 4: the "LiteLLM Gateway" box becomes the policy gateway with a model adapter behind it. Budget enforcement and model selection by role are done by the gateway and OPA, not by LiteLLM virtual keys.
- Table 17: "Budget enforcement: LiteLLM virtual keys" becomes "registry limits enforced by OPA, counters in the key-value store".
- Section 6.1.1: "LiteLLM gates" should read "policy gateway with a model adapter built on the LiteLLM library".
- Table 14: add prompt egress as a residual risk, and qualify log tampering detection with the rotation finding and the independent verifier.

## 9. Next steps

| No. | Item |
|---|---|
| 1 | Run Stage 3 on the Kali box: build, both acceptance tests, agent probe, ledger verifier |
| 2 | Add the Anthropic key and run the provider test once (`PROVIDER_TEST=1`) |
| 3 | Run `./scripts/latency-run.sh gateway` and keep the results for Chapter 7 |
| 4 | Report the rotation finding and the rate limit to Miracle Owolabi |
| 5 | Replace the example registry with the real purposes, tools, models, and limits |
| 6 | Telemetry: OpenTelemetry Collector, completing the Observability Plane |
| 7 | Restrict the adapter's egress to the provider |
| 8 | Dependency audit of ARTO's pinned packages and of the adapter's set |
| 9 | Chapter 6 and Chapter 7 corrections |
