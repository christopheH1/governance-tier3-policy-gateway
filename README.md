# Policy gateway build: Stage 6 (tool path, model path, telemetry, agents, console, workflows, local model)

Open-source Tier 3 build. Every tool call and every model call passes through a policy gateway that
asks who the caller is and what it intends, then denies, refers to a human, or allows.

## Layout of the running system

```
  agents network          control network                 tools / models networks      egress
  --------------          ---------------                 -----------------------      ------
  agent ---> provisioner ---> Tessera, Vestigia, OPA
        |
        +--> policy gateway ---> Tessera, OPA, Vestigia, Valkey
                      |-----------------------------------> mock tools
                      \-----------------------------------> model adapter -----------> provider
```

| Service | Component | Role |
|---|---|---|
| `policy-gateway` | Own code (`gateway/`) | Enforcement point for both paths: identity, intent, decision, audit, then the call |
| `provisioner` | Own code (`provisioner/`) | Only holder of the Tessera admin key. Enrols agents, issues tokens |
| `model-adapter` | Own code (`model-adapter/`) on the LiteLLM library | Only holder of provider keys, and the only container with a route out |
| `tessera` | ARTO Tessera, pinned commit | Tokens, DPoP binding, revocation |
| `vestigia` | ARTO Vestigia, pinned commit, one patch | Hash-chained audit ledger (JSON file) |
| `opa` | Open Policy Agent 1.19.1 | Decisions for both paths. Serves the registry |
| `valkey` | Valkey 9.0.6 | Hold state, flag queue, usage counters, replay and revocation caches |
| `otel-collector` | OpenTelemetry Collector 0.162.0 | Observability Plane: traces and metrics from the gateway |
| `mock-tools` | Own code (`mock-tools/`) | Stand-in tools that count their calls |
| `agent-runtime` | Own code (`agent-runtime/`) | Hosts the two agents of the Research Operations scenario. Holds their private keys only |
| `console` | Own code (`console/`) | Web page for the operator and the reviewer, at http://127.0.0.1:8088 |

Every network except `egress` and `ui` is internal. The only published port is the console, on this machine only. Tests run from containers.

## Upgrade from Stage 5

```bash
cd ~/Projects
tar -xzf ~/Downloads/governance-tier3-stage6-r8.tar.gz
cd governance-tier3
./scripts/gen-secrets.sh            # adds anything missing, changes nothing that exists
docker compose build
docker compose up -d --wait
docker compose restart opa          # load the new policy and registry
```

A first install also needs `./scripts/bootstrap.sh` before the build.

To use the external provider, edit `.env`, set `ANTHROPIC_API_KEY=` to your key, and run
`docker compose up -d model-adapter`. Without a key the mock model still works and everything else is unaffected.

## The console

Open http://127.0.0.1:8088 in a browser on this machine and sign in with your own account.

Each person has their own account, with a name, a password, and roles. Create yours first:

```bash
./scripts/console-user.sh add christophe                 # all three roles. The password is printed once
./scripts/console-user.sh add mei --roles reviewer       # someone who only reviews
./scripts/console-user.sh list
```

| Role | May |
|---|---|
| `requester` | Give agents tasks and reply to them |
| `reviewer` | Approve or reject held calls |
| `operator` | Enrol agents |

The name you sign in with is passed on with everything you do. The audit ledger records it on every
model call and tool call an agent makes for you ("on behalf of"), on each flag, and on each review
decision. So the trail shows who asked, which agent acted, and who reviewed.

To require a second person for approvals, add `REVIEW_FOUR_EYES=true` to `.env` and run
`docker compose up -d policy-gateway`. Nobody can then approve a call made on their own behalf.

The automated tests sign in with three accounts of their own, which share `CONSOLE_PASSWORD` from
`.env`. No other name can use that password. Once testing is done, add `CONSOLE_TEST_ACCOUNTS=false`
to `.env` and run `docker compose up -d console` to switch those accounts off.

1. Choose **Enrol the agents** once. This is the operator registering each agent's public key and role.
2. Pick an agent, a purpose, and a model, type a task, and choose **Start the task**.
3. Watch each step appear with the gateway's decision stamped on it: allowed, denied, held for review, or flagged.
4. When a call is held, a band appears at the top. **Approve** or **Reject** it, and the agent carries on.
5. When the agent has answered, you can **reply** under its answer. The agent carries on with the same task, and your reply reaches the model through the gateway like any other call.

Two agents are set up, in `agent-runtime/agents.json`:

| Agent | Role | Tools it knows | What you will see |
|---|---|---|---|
| Data analyst | `analyst` | `database_read`, `file_read`, `data_export` | The gateway hides `data_export` from the model, since the analyst's role does not include it |
| Report writer | `report_writer` | `report_generation`, `data_export`, `database_delete` | `database_delete` is hidden. `data_export` is held until a reviewer decides |

Two models can be chosen. `scripted-model` is a stand-in that calls each tool it is offered once,
whatever the task says, and needs no provider key. `claude-haiku` is the real provider: it reads
the task and decides for itself, and the task text leaves the organisation.

## Workflows in LangGraph, and a local model

Stage 6 lets a workflow application build and run the agents. Such applications speak two standard
protocols, and the agent runtime now offers both as a **bridge** to the policy gateway:

| For | Address (inside Docker) | Protocol |
|---|---|---|
| Models | `http://agent-runtime:9070/v1` | OpenAI chat format |
| Tools | `http://agent-runtime:9070/mcp` | Model Context Protocol over HTTP |

The application presents one API key per agent. The bridge adds the agent's token, its proof of key,
and the declared purpose, and passes the call to the policy gateway. The bridge decides nothing.
Two optional headers: `X-Purpose` (otherwise the agent's first purpose) and `X-Task-Id` (so several
agents share one task in the audit trail).

Three models can be chosen per agent. The gateway enforces the choice against the agent's role.

| Model | Where it runs |
|---|---|
| `local-model` | Inside the organisation, on Ollama. Nothing leaves |
| `claude-haiku` | The external provider. The task text leaves the organisation |
| `scripted-model` | A stand-in for repeatable tests |

### One-time setup

```bash
./scripts/gen-secrets.sh                                   # adds the bridge secret and the agents' API keys
docker compose build workflow                              # the LangGraph image
docker compose --profile setup run --rm ollama-pull        # downloads the local model, about 2 GB
docker compose --profile workflow up -d --wait
```

The local model is `llama3.2:3b` unless `OLLAMA_MODEL` in `.env` says otherwise. Ollama then runs on
the models network only, with no route out.

### LangGraph: agents in code

`workflows/research_ops.py` is a two-agent workflow: the data analyst gathers figures and hands
them to the report writer. Each agent has its own identity. Enrol the agents in the console first.

```bash
docker compose run --rm workflow python research_ops.py \
  "Report the Q4 sales figures by region and send the report out." \
  --analyst-model local-model --writer-model claude-haiku
```

While it runs, open the console: each step appears there, and the writer's export waits for you
to approve or reject it. The workflow waits about 40 seconds for a reviewer, then carries on and
reports that the export is still waiting. The command prints a run identifier for the audit trail:

```bash
docker compose --profile test run --rm ledger-verify python /tools/audit_trail.py --task wf-...
```

## Checking that a request fits its declared purpose

The declared purpose is a claim. Policy checks the purpose code and cannot read the request, so an
approved purpose can be declared for an unrelated request. After a model call is allowed, the gateway
therefore asks a model inside the organisation whether the newest request fits the purpose's
description, and flags a mismatch for review.

- **It never blocks.** The call goes ahead. The check runs beside it.
- **It looks at every new request**, including each reply, once per distinct request in a task.
- **Its verdict is written to the ledger** as `INTENT_CHECK`: fits, mismatch, unclear, or not checked, with the checker's short reason and the request's fingerprint.
- **The words of a flagged request are kept with the flag**, up to 2,000 characters, because a flag is the case where someone needs to read what was asked. The words of other requests and replies are not kept. Set `intent_check.record_flagged_text` to `false` in `policies/data.json` to keep fingerprints only. Text once written to the ledger cannot be removed without breaking the chain.
- **A mismatch appears in the console** under "Calls that ran and were flagged".
- **Nothing leaves for the sake of the check.** The checking model runs inside, on a network with no route out.
- **It is fallible.** The checker is a small model and can be wrong in both directions.

The checking model is part of the control plane. It runs in its own Ollama (`ollama-checker`) on the
control network, beside the gateway, which calls it directly. Agents cannot reach it, it is not one
of the models agents may use, and it does not share a server with the model agents use. Set it up once:

```bash
docker compose --profile setup run --rm ollama-checker-seed    # copies the model already downloaded, no network
docker compose --profile workflow up -d --wait                 # starts ollama-checker with the rest
```

To give the checker a different model from the agents', set `CHECKER_MODEL` in `.env` and use
`ollama-checker-pull` in place of the seed step. To run the checker without the agents'
Ollama, use `--profile checker`. Without a running checker, each request is recorded as "not checked".

Settings are in `policies/data.json`: `intent_check.enabled` switches the check on, and each purpose
has a `description`, which is what the checker judges against. Restart OPA after changing them.

## Tests

```bash
docker compose --profile test run --rm e2e                          # 51 checks: tool path
docker compose --profile test run --rm e2e python e2e_models.py     # 29 checks: model path and the purpose-fit check, mock model, no cost
docker compose --profile test run --rm e2e python e2e_agents.py     # 29 checks: both agents, accounts and roles, a reply, and who asked
docker compose --profile test run --rm e2e python e2e_bridge.py     # 23 checks: the bridge a workflow application uses
docker compose --profile test run --rm workflow-test                # 9 checks: the LangGraph workflow (after 'docker compose build workflow')
PROVIDER_TEST=1 docker compose --profile test run --rm e2e python e2e_models.py   # adds one short provider call
docker compose --profile test run --rm agent-probe                  # 10 checks: what an agent can and cannot reach
./scripts/failclosed-test.sh                                        # 9 checks: each dependency stopped in turn
docker compose --profile test run --rm ledger-verify                # independent check of the audit ledger
docker compose --profile test run --rm telemetry-show               # what the collector has recorded
docker compose --profile test run --rm telemetry-show python /tools/telemetry_summary.py --dir /telemetry --check
docker compose --profile test run --rm test-runner                  # 23 checks: Stage 1 foundation
```

## Latency measurements for Chapter 7

```bash
./scripts/latency-run.sh gateway                 # per-stage latency, tool path and model path
./scripts/latency-run.sh ledger-curve            # audit write time against ledger size, up to an hour
./scripts/reset-ledger.sh                        # lab only: start again from an empty ledger
```

Results are saved under `results/` as a `.json`, a `.txt`, and a `.meta.txt`. Options: `--requests`,
`--warmup`, `--workers 1,2,4`, `--scenarios`, and for the curve `--target-entries`, `--max-minutes`.
The gateway reports time per stage in a `Server-Timing` header: `identity`, `store`, `policy`,
`audit_decision`, `tool` or `model`, `audit_result`, `total`.

- Audit write time depends on ledger size, so every row shows the ledger size before and after.
- Load is closed-loop. The requests-a-second figure is what the stack sustained.
- The `model_allow` scenario uses the mock model, so it shows gateway overhead without provider time.

## How a call works

1. An operator enrols the agent with a role and the agent's public key (`POST provisioner/v1/agents`).
2. The agent proves it holds the private key and receives a token: one per tool, and one for the model path (`model_access`).
3. Tool path: `POST policy-gateway/v1/tools/invoke` with token, DPoP proof, intent, tool name, and parameters.
   Deny, allow, refer and flag, or refer and hold until a reviewer approves that exact request.
4. Model path: `POST policy-gateway/v1/models/chat` with token, DPoP proof, intent, model name, and messages.
   Policy checks the model against the role, the token ceiling, the rate limit, and the daily token budget,
   and removes any offered tool the agent could not itself use. Deny or allow.
5. Every decision is written to the ledger before anything runs. For model calls the ledger holds the size
   and fingerprint of the prompt, not its text.

Reviewer endpoints, protected by `GATEWAY_REVIEWER_KEY`:
`GET /v1/review/holds`, `POST /v1/review/holds/{id}/decision`, `GET /v1/review/flags`.

## Telemetry

The gateway sends traces and metrics to the OpenTelemetry Collector, which writes them to the
`telemetry-data` volume as JSON lines. `telemetry-show` reads those files and prints a summary.

- **One trace per request**, with a child span for each stage. The trace identifier is the same
  `trace_id` the gateway returns and writes to the ledger, so a ledger entry can be matched to its trace.
- **Sampling happens in the collector:** every denied, referred, or failed request is kept, and about
  one in ten allowed requests. Metrics count every request.
- **Telemetry fails open.** It is sent in the background, and a collector that is down never delays or
  blocks a request. The ledger, which fails closed, remains the record of what was decided.
- **What is sent:** outcome, reasons, agent, role, tool or model name, and stage timings. Tool
  parameters and prompt content are never sent.
- **Not included in Profile 3:** a Prometheus server, dashboards, or SIEM export. The collector exposes
  metrics for scraping on port 8889 of the control network for when those are added.

## Checking the ledger independently

`tools/verify_ledger.py` reads the ledger files directly and recomputes every hash. It runs with no
network and does not call Vestigia. Unlike Vestigia's built-in check, it follows the chain across
rotation, so it remains valid after the ledger passes 10,000 entries. It prints the head hash:
record that somewhere outside this machine, since no verifier can notice that the newest entries
were removed unless it knows what the head should be.

## Folders

```
governance-tier3/
├── docker-compose.yaml        Services, networks, test profiles
├── versions.lock              Pinned commit, image digests, package pins, patches
├── .env                       Generated secrets and your provider key (not in version control)
├── docker/                    Dockerfiles and pinned requirements
├── gateway/                   Policy gateway
├── provisioner/               Provisioning service
├── model-adapter/             Model adapter and its routes
├── mock-tools/                Stand-in tools
├── agent-runtime/             The agents and their definitions
├── console/                   Web console for operator and reviewer
├── workflows/                 LangGraph workflow and its test
├── observability/             OpenTelemetry Collector configuration
├── policies/                  Rego policies, example registry, policy tests
├── scripts/                   bootstrap, gen-secrets, failclosed-test, latency-run, reset-ledger
├── tests/                     Acceptance tests, probes, latency harness
├── tools/                     verify_ledger.py, telemetry_summary.py
├── results/                   Saved measurement results (not in version control)
└── vendor/ARTO/               Pinned upstream clone (not in version control)
```

## Things to know

- **Secrets are stable.** Changing `VESTIGIA_SECRET_SALT` invalidates the ledger chain, and changing `TESSERA_SECRET_KEY` invalidates every token.
- **Prompt content leaves the organisation on every external model call.** Tier 3 does not read prompts. It limits who may call which model, how much, and with which tools on offer.
- **The model adapter can reach any internet address.** Restricting it to the provider's address needs a host firewall rule or a forwarding proxy, which this build does not include.
- **One patch is applied to upstream code.** `docker/patch_vestigia.py` makes Vestigia's rate limit configurable. See `versions.lock`.
- **The ledger is the throughput ceiling.** Every write rewrites the whole file, so write time grows with ledger size.
- **Vestigia's own integrity check fails after rotation at 10,000 entries.** Use `ledger-verify`. The acceptance tests' integrity check will fail from that point until the ledger is reset.
- **Policy and registry changes need `docker compose restart opa`.**
- **`policies/data.json` is example data** and contains test-only roles, a tool, and a model.
- **Reviewer and operator access is a shared key each.** Tie both to real identities before any use outside the lab.
- **The console is for one machine.** It is published on 127.0.0.1 only. The agents share a network with it, so the accounts' passwords are what protect it from them.
- **The person's name is verified at the console, not at the gateway.** The gateway verifies the agent and records the person's name as the agent runtime states it. A task started by a workflow application carries a name only if the application supplies one, and that name is not verified.
- **Delegation is a hand-over in the workflow, not a delegated credential.** The analyst's findings are passed to the writer, each acts under its own identity, and both share one task identifier. No agent acts with another's rights.
- **A workflow application holds the agents' API keys.** It can act as any agent it has a key for, within that agent's role. It never holds an agent's private key.
- **No streaming on the model path.** One gateway instance, one Tessera instance.
- **Usage counters** (requests a minute, tokens a day) are kept in Valkey and reset with it.
