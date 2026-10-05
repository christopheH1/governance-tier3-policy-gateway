#!/usr/bin/env bash
# Ensures .env holds every secret the stack needs.
# Existing values are never changed, because the Vestigia salt and the Tessera secret
# must stay stable once data exists. Missing values are generated and appended.
set -euo pipefail
cd "$(dirname "$0")/.."

umask 077
touch .env
chmod 600 .env
python3 - <<'PY'
import secrets

wanted = {
    "TESSERA_SECRET_KEY": lambda: secrets.token_hex(64),        # 64 bytes, as Tessera requires for HS512
    "TESSERA_ADMIN_KEY": lambda: secrets.token_urlsafe(48),     # held by the provisioner only
    "VESTIGIA_API_KEY": lambda: secrets.token_urlsafe(48),
    "VESTIGIA_SECRET_SALT": lambda: secrets.token_hex(32),
    "VALKEY_PASSWORD": lambda: secrets.token_hex(32),           # hex only, so it is safe inside a URL
    "PROVISIONER_OPERATOR_KEY": lambda: secrets.token_urlsafe(48),
    "GATEWAY_REVIEWER_KEY": lambda: secrets.token_urlsafe(48),
    "MODEL_ADAPTER_KEY": lambda: secrets.token_urlsafe(48),     # shared by the gateway and the model adapter only
    "AGENT_RUNTIME_KEY": lambda: secrets.token_urlsafe(48),     # shared by the console and the agent runtime only
    "BRIDGE_SECRET": lambda: secrets.token_urlsafe(48),         # the agents' API keys for workflow applications are derived from it
    "CONSOLE_PASSWORD": lambda: secrets.token_urlsafe(12),      # for the automated tests' console accounts only. People have their own accounts
    "ANTHROPIC_API_KEY": lambda: "",                            # left empty: paste your provider key here yourself
}
with open(".env") as handle:
    present = {line.split("=", 1)[0] for line in handle if "=" in line and not line.startswith("#")}
added = [name for name in wanted if name not in present]
with open(".env", "a") as handle:
    for name in added:
        handle.write(f"{name}={wanted[name]()}\n")
# One API key per agent, derived from BRIDGE_SECRET exactly as the agent runtime derives it.
# A workflow application is given these keys. It never holds an agent's private key.
import hashlib, hmac, json
with open(".env") as handle:
    values = dict(line.rstrip("\n").split("=", 1) for line in handle if "=" in line and not line.startswith("#"))
with open("agent-runtime/agents.json") as handle:
    agent_ids = [agent["id"] for agent in json.load(handle)["agents"]]
with open(".env", "a") as handle:
    for agent_id in agent_ids:
        name = "AGENT_KEY_" + agent_id.upper().replace("-", "_")
        if name not in values:
            key = "agk_" + hmac.new(values["BRIDGE_SECRET"].encode(), agent_id.encode(), hashlib.sha256).hexdigest()[:40]
            handle.write(f"{name}={key}\n")
            added.append(name)
print("added: " + ", ".join(added) if added else "nothing to add: .env already complete")
PY
