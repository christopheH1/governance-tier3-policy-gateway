# Policy gateway: Stage 5 build notes (agents and console)

| Field | Value |
|---|---|
| Date | 4 October 2026 |
| Archive | `governance-tier3-stage5.tar.gz` |
| Scope | Two agents for the paper's Research Operations scenario, and a web console for the operator and the reviewer |
| Status | Runs on the Kali box under Docker: 18 of 18 with the scripted model. Console not yet confirmed in a browser |

## 1. What Stage 5 adds

```
 browser on the Kali box
        |  http://127.0.0.1:8088  (the only published port)
        v
  +-----------+        +----------------+
  |  console  |------->| agent-runtime  |   holds the agents' private keys
  +-----------+        +----------------+
     |      |               |        |
     |      |  enrol        | tokens | model calls and tool calls
     |      v               v        v
     |   +-------------+   +------------------+
     |   | provisioner |   |  policy gateway  |---> OPA, Tessera, Vestigia, Valkey
     |   +-------------+   +------------------+
     |  review decisions        ^   |       |
     +--------------------------+   v       v
                              tools     model adapter ---> provider
```

| Service | Code | Role | Networks | Holds |
|---|---|---|---|---|
| `agent-runtime` | `agent-runtime/app.py`, `agents.json` | Runs each agent's loop: ask the model through the gateway, call the tools the model asks for through the gateway, wait when a call is held | agents | The agents' EC P-256 private keys (volume `agent-keys`) |
| `console` | `console/app.py`, `index.html` | Web page: enrol agents, start a task, watch each step, approve or reject held calls, see flagged calls | agents, ui | Operator key, reviewer key, runtime key. No agent key |

The agent runtime has no privileged credential. Everything an agent does goes through the provisioner (tokens) and the policy gateway (model and tool calls), exactly as the test clients did in Stages 2 to 4.

## 2. The two agents

| Agent | Role | Purposes | Tools it knows | Expected behaviour |
|---|---|---|---|---|
| `data-analyst-01` | `analyst` | `sales_reporting`, `market_research` | `database_read`, `file_read`, `data_export` | `data_export` is outside the role: the gateway removes it from the tools shown to the model |
| `report-writer-01` | `report_writer` | `report_delivery`, `report_writing` | `report_generation`, `data_export`, `database_delete` | `database_delete` is removed. `data_export` is referred and held until a reviewer decides |

Each agent is deliberately told about one tool its role does not include, so that the console shows the gateway enforcing the role and not the agent behaving well.

Registry additions in `policies/data.json`: role `report_writer`, purpose `report_delivery`, model `scripted-model`. The Rego policies are unchanged (29 of 29 policy tests pass).

## 3. Models

| Model | What it is | Use |
|---|---|---|
| `scripted-model` | A stand-in inside the model adapter. It calls each tool it is offered once, in order, then reports. It ignores the task text | Repeatable demonstrations and tests with no provider key and no cost |
| `claude-haiku` | The external provider | A real agent that reads the task and chooses its own tool calls |

## 4. The console

- Published on `127.0.0.1:8088` only. Sign in with any name and the shared `CONSOLE_PASSWORD` from `.env`. The name is recorded as the reviewer on each decision.
- A review band appears at the top only when a call is waiting. It shows the agent, the tool, the parameters, the declared purpose, the reason for the hold, and the time at which the hold becomes a denial.
- The transcript lists each model call and tool call in order with the gateway's outcome (allowed, denied, held for review, allowed and flagged), the tools the gateway removed, tokens used, and the trace identifier shared with the ledger and telemetry.
- Changes of state need an `X-Console` header as well as the password, which stops another web page in the same browser from submitting them. The page builds its content as text only, so tool output cannot inject markup.

## 5. Test results

| Test | Where | Result |
|---|---|---|
| `tests/e2e_agents.py`, 18 checks, through the console | Development container, local processes | 18 of 18 |
| Policy tests | Development container | 29 of 29 |
| Console page at 1,280 and 420 pixels wide | Development container, headless browser | Rendered and reviewed. One narrow-width layout fault found and fixed |
| `tests/e2e_agents.py` under Docker | Kali box, 4 October 2026 | 18 of 18. All eleven services healthy |

## 6. Not verified, and limits

1. **The published port is unconfirmed.** The services, the key volume, and the networks work under Docker, but the test reaches the console over the agents network. Opening the page in a browser is the remaining check.
2. **The `ui` network setting is unconfirmed** for the same reason. It switches off address translation so the console has no route out. If the console's port does not answer on the Kali box, remove the `driver_opts` lines under `ui` in `docker-compose.yaml` and say so in the notes.
3. **The agent loop has not been run with the real provider.** Only the scripted model was tested. Tool calling with `claude-haiku` through the adapter is expected to work but is unproven.
4. **Agents do not delegate.** Each works alone in its own role. The paper's scenario implies a hand-over from analyst to writer: here the person starts each task.
5. **The tools are mock tools** with a small fixed sales data set.
6. **The console is a single-machine demonstration.** One shared password, no separate operator and reviewer accounts, and it holds both the operator key and the reviewer key. The agents share a network with it, so the password is the only thing between an agent and the approve button. A production design would put the console on its own network and give reviewers individual identities.
7. The agent isolation probe was not extended to cover the console.

## 7. Differences from the paper to record

1. The paper does not describe a reviewer interface. A refer outcome needs one: Stage 5 supplies a minimal one.
2. Segregation of duties between operator (enrolment) and reviewer (approval) is not enforced in this console.
3. Delegation between agents, with its depth limit, is not built.

## 8. Demonstrating the audit trail (added 4 October 2026, 16:10)

`tools/audit_trail.py` reads the ledger files directly, with no network and no call to Vestigia, and reuses the independent verifier's hash code. It runs in the existing `ledger-verify` container.

| Command (prefix: `docker compose --profile test run --rm ledger-verify python /tools/audit_trail.py`) | Shows |
|---|---|
| no argument | The tasks in the ledger |
| `--last`, `--task <id>`, or `--trace <id>` | Every ledger entry of one task in order: decision, reasons, tools removed, reviewer, fingerprints, credential, trace. Each entry's hash is recomputed, then the whole chain is verified |
| `--tamper-demo` | Copies the ledger, alters the copy three ways (rewrite a refusal to ALLOWED, delete an entry, rewrite and re-hash without the salt), and shows the verifier rejecting each. The real ledger is read-only in that container |

The trace identifier shown on each step in the console is the link from the page to the ledger: `--trace` accepts it.

Tested in the development container on a ledger of 58 entries: trail of a held export shown, 3 of 3 alterations detected. Not yet run on the Kali box. The approve and reject entries were not in that test ledger, so their display is untested.

Limits to state to an observer:
1. The chain proves that nothing inside it was changed, removed, or inserted. It does not prove that the end was not cut off, unless the head hash was recorded somewhere else beforehand.
2. Someone holding both the ledger file and the salt could rebuild the whole chain. The protection is that agents and tools cannot reach either. External anchoring of the head hash is not built.
3. The ledger records fingerprints of prompts and responses, not their text.
