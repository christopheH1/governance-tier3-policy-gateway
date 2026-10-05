# Policy gateway: Stage 1 build notes

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Status | Build files delivered. Not yet run under Docker on the Kali box |
| Build location | `~/Projects/governance-tier3` |
| Companion | `claude/GATEWAY_INTERCEPTION_DECISION_RECORD.md` (revision 3) |
| Supersedes | Section 13, items 1 to 4 of the decision record |

## 1. Decisions taken since revision 3 of the decision record

| Item | Decision |
|---|---|
| Audit store of record | Option A: Vestigia's JSON file ledger as it ships. Chapter 6 is to be corrected. PostgreSQL is not part of Profile 3 |
| Tessera server | `api_server.py`. The file `api_server_production.py` lacks delegation, agent management, and key rotation endpoints |
| Key-value store | Valkey 9.0.6, password-protected |
| Stage 1 scope | Tessera, Vestigia, OPA, Valkey, and a test runner. The policy gateway, LiteLLM, and the telemetry collector come in later stages |
| Provisioning model (Stage 2) | A separate provisioning service holds the Tessera admin key, registers agents, and issues their per-tool tokens. The policy gateway does not hold the admin key |

### 1.1 Provisioning model: reasoning and consequences

Reason: separation of duties. The component that enforces policy is not also the one that issues credentials, so a compromise of the gateway does not allow it to mint identities or tokens.

```
  Provisioner ---- admin key ----> Tessera   (register agent, request token)
       |
       | token (one per tool), bound to the agent's DPoP public key
       v
     Agent ---- token + DPoP proof + intent ----> Policy gateway
                                                       |
                                                       | validate (no admin key)
                                                       v
                                                    Tessera
```

Consequences for the build:

- The Tessera admin key is given to the provisioner only. It is removed from the gateway's environment and never reaches agents.
- The agent generates its own key pair and sends only the public key to the provisioner. Private keys stay with the agent.
- The provisioner issues a token only for tools in the agent's role, taken from the same registry data that OPA uses, so the Tessera whitelist and the policy cannot drift.
- Three parties may reach Tessera: the provisioner (registration, issuance, revocation), the policy gateway (validation), and nothing else. Agents have no route to it.
- Revocation on Tessera is unauthenticated, so the revoke action is exposed to operators through the provisioner, which applies its own authorisation.
- Every provisioning action (agent registered, token issued, token revoked) is written to Vestigia.

Open design points for Stage 2: how an agent authenticates to the provisioner, token lifetime and renewal, and whether the provisioner sits on its own network segment.

## 2. What Stage 1 contains

```
governance-tier3/
├── docker-compose.yaml        Stage 1 services, internal network, no published ports
├── versions.lock              Pinned commit, image digests, package pins
├── docker/                    Dockerfiles and pinned requirement subsets
├── policies/                  tool_egress.rego, data.json (example), tool_egress_test.rego
├── scripts/                   bootstrap.sh (pin ARTO), gen-secrets.sh (write .env)
├── tests/                     smoke_foundation.py (23 checks)
└── vendor/ARTO/               Pinned upstream clone, commit b57be5e
```

Deployment: extract the archive from `~/Projects`, since its top-level folder is `governance-tier3`.

Run sequence: `./scripts/bootstrap.sh`, `./scripts/gen-secrets.sh`, `docker compose build`, `docker compose up -d`, `docker compose --profile test run --rm test-runner`.

## 3. What has been verified, and how

| Item | Result | How |
|---|---|---|
| Tool-path policy | `opa check --strict` clean, 13 of 13 policy tests pass | OPA 1.19.1 binary |
| Tessera and Vestigia with ARTO's pinned packages | Both start and serve | Python 3.11, API-only subset of ARTO's requirement pins |
| Acceptance test | 23 of 23 checks pass | Services run as plain processes, not in containers |
| Key-value store with password | Tessera wrote revocation, replay, rate-limit, and session keys | Redis 7.0.15, not Valkey |
| Compose file and scripts | Syntax valid. `bootstrap.sh` and `gen-secrets.sh` executed successfully | YAML parse, script runs |

Not verified: the Docker images and the compose stack themselves (no Docker daemon was available), and Tessera against a real Valkey. The first run on the Kali box is the test of both. The acceptance test prints the store's name and version.

## 4. Findings from running the services

1. **A token covers one tool.** The access token carries a `tool` claim, and validation for any other tool is refused. An agent needs one token per tool it uses.
2. **The gateway can forward the agent's DPoP proof.** `/tokens/validate` accepts `expected_htu` and `expected_htm`, so the agent signs its proof for the gateway URL and the gateway states where it was presented. A proof for another URL is refused, and a replayed proof is refused.
3. **Self-registration and token minting are open by default.** `TESSERA_REQUIRE_REGISTRATION_AUTH=true` closes this. Token issuance then needs the admin key, which is why a provisioning service is needed (see 1.1).
4. **Some Tessera endpoints accept unauthenticated calls even in strict mode.** `/tokens/revoke`, `/tokens/validate`, and `/agents/list` returned HTTP 200 with no key. Tessera must therefore be reachable only by the policy gateway and the provisioner, never by agents.
5. **Vestigia records every API request as its own ledger entry.** One gateway audit write produces two entries, and each entry rewrites the whole ledger file. Write time measured at about 10 ms on a near-empty ledger. Growth with ledger size must be measured for Chapter 7, and rotation settings chosen.
6. **Tamper detection works.** Editing one field in the ledger file made `/integrity` report a hash mismatch at the altered entry, and `/health` reported `ledger_valid: false`.
7. **Tessera starts slowly without the key-value store.** About 25 seconds without it, about 1 second with it.
8. **Strict production mode also switches on memory binding.** Token requests must carry `session_id` and `memory_state`.
9. **A policy alias defeated a test override.** Referring to registry data through a rule alias stopped a `with data...` override from applying in tests. The policy references `data.tier3` directly.

## 5. Tool-path policy as built

Package `ai_governance.tier3.tool_egress`, rule `decision`. Registry data under `data.tier3` (`roles`, `purposes`, `tools`, `referral`).

| Outcome | Reasons implemented |
|---|---|
| Deny | `identity_not_verified`, `tool_not_registered`, `tool_not_whitelisted`, `intent_not_declared`, `task_not_declared`, `intent_not_recognised`, `intent_tool_mismatch`, `no_matching_rule` |
| Refer | `sensitive_tool`, `row_threshold`, `tool_unclassified`. Mode is `flag` for class `reversible_read`, otherwise `hold` |
| Allow | `all_constraints_satisfied` |

`policies/data.json` holds example data only.

## 6. Consequences for the paper

- Chapter 6: "Redis-backed instant revocation" and "hash-chained PostgreSQL logs" to be corrected (see decision record, section 12).
- Chapter 6, Table 21: the Foundational cost profile lists PostgreSQL. With Option A it is not needed.
- Chapter 6, Figure 4 and Section 6.2.2.3: add the provisioning service as a component distinct from the policy gateway.
- Chapter 7: report the audit write cost per decision as two ledger entries, and the provisioning model for tokens.

## 7. Next steps

| No. | Item |
|---|---|
| 1 | Run Stage 1 on the Kali box and record the result, including the Valkey version line |
| 2 | Confirm image digests on the build host with `docker buildx imagetools inspect` |
| 3 | Stage 2: write the provisioning service and the policy gateway (tool path first), with the hold store in Valkey and synchronous audit writes |
| 4 | Settle the open provisioning design points in 1.1 |
| 5 | Replace the example registry with the real purpose catalogue and tool classification |
| 6 | Stage 3: model path through LiteLLM (MIT tree), with an egress network attached to the gateway only |
| 7 | Dependency audit of ARTO's pinned packages |
