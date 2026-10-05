#!/usr/bin/env bash
# Prints what a workflow application needs for each agent: its API key, purposes, models, and tools.
# The keys are derived from BRIDGE_SECRET in .env. Treat them like passwords.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T agent-runtime python - <<'PY'
import json, os, urllib.request
request = urllib.request.Request("http://127.0.0.1:9070/v1/bridge/keys",
                                 headers={"Authorization": "Bearer " + os.environ["AGENT_RUNTIME_KEY"]})
for agent in json.load(urllib.request.urlopen(request))["agents"]:
    print(f"{agent['name']}  ({agent['id']})")
    print(f"  API key:   {agent['api_key']}")
    print(f"  purposes:  {', '.join(agent['purposes'])}   (the first is used unless an X-Purpose header says otherwise)")
    print(f"  models:    {', '.join(agent['models'])}")
    print(f"  tools:     {', '.join(agent['tools'])}")
    print()
print("For a workflow application inside Docker:")
print("  model base path:  http://agent-runtime:9070/v1")
print("  tool server:      http://agent-runtime:9070/mcp")
PY
