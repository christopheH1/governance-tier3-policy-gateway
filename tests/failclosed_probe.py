"""Fail-closed probe, driven by scripts/failclosed-test.sh.

  setup         enrol one agent, obtain a token, and prove a normal call is allowed
  expect-deny   make the same call while a dependency is stopped: it must be denied
                and must not reach the tool
  expect-allow  make the call again after the dependency is back: it must be allowed

State (the agent's test key and token) is kept in /state between phases.
"""
import json
import os
import sys
import time
import uuid

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

GATEWAY = os.environ.get("GATEWAY_URL", "http://policy-gateway:9090")
PROVISIONER = os.environ.get("PROVISIONER_URL", "http://provisioner:9080")
MOCK_TOOLS = os.environ.get("MOCK_TOOLS_URL", "http://mock-tools:8000")
STATE = os.environ.get("PROBE_STATE", "/state/probe.json")
INVOKE_URL = f"{GATEWAY}/v1/tools/invoke"
TOKEN_URL = f"{PROVISIONER}/v1/tokens"
INTENT = {"purpose": "sales_reporting", "task_id": "failclosed-probe"}


def proof(key, url):
    numbers = key.public_key().public_numbers()
    b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
    jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
    return jwt.encode({"htu": url, "htm": "POST", "iat": int(time.time()), "jti": uuid.uuid4().hex},
                      key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})


def tool_calls():
    return requests.get(f"{MOCK_TOOLS}/_calls", timeout=5).json().get("database/read", 0)


def call(key, token):
    try:
        resp = requests.post(INVOKE_URL, timeout=30,
                             headers={"Authorization": f"DPoP {token}", "DPoP": proof(key, INVOKE_URL)},
                             json={"intent": INTENT, "tool": {"name": "database_read", "params": {"limit": 1}}})
        return resp.status_code, resp.json()
    except (requests.RequestException, ValueError) as exc:
        return 0, {"outcome": "no answer", "reasons": [type(exc).__name__]}


def setup():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    agent_id = f"probe-{uuid.uuid4().hex[:8]}"
    operator = {"Authorization": f"Bearer {os.environ['PROVISIONER_OPERATOR_KEY']}"}
    r = requests.post(f"{PROVISIONER}/v1/agents", headers=operator, timeout=15,
                      json={"agent_id": agent_id, "role": "analyst", "owner": "failclosed-probe", "public_key_pem": pem})
    r.raise_for_status()
    r = requests.post(TOKEN_URL, headers={"DPoP": proof(key, TOKEN_URL)}, timeout=15,
                      json={"agent_id": agent_id, "tool": "database_read"})
    r.raise_for_status()
    private_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
    with open(STATE, "w") as handle:
        json.dump({"key": private_pem, "token": r.json()["token"]}, handle)
    status, answer = call(key, r.json()["token"])
    ok = answer.get("outcome") == "allow"
    print(f"[{'PASS' if ok else 'FAIL'}] baseline: call is allowed with every dependency running  (HTTP {status})")
    return ok


def expect(phase, label):
    with open(STATE) as handle:
        state = json.load(handle)
    key = serialization.load_pem_private_key(state["key"].encode(), password=None)
    before = tool_calls()
    status, answer = call(key, state["token"])
    reached = tool_calls() - before
    if phase == "expect-deny":
        ok = answer.get("outcome") == "deny" and reached == 0
        print(f"[{'PASS' if ok else 'FAIL'}] {label} stopped: call is denied and the tool is not reached  "
              f"(HTTP {status}, {answer.get('reasons')})")
    else:
        ok = answer.get("outcome") == "allow" and reached == 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label} restored: call is allowed again  (HTTP {status})")
    return ok


if __name__ == "__main__":
    phase = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    sys.exit(0 if (setup() if phase == "setup" else expect(phase, label)) else 1)
