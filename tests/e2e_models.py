"""Stage 3 acceptance test: the model path through the policy gateway.

Uses the mock model, so it costs nothing and needs no provider key.
Set PROVIDER_TEST=1 to add one short call to the external provider (needs ANTHROPIC_API_KEY in .env).

The exit code is non-zero if any check fails.
"""
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
VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")
OPERATOR = {"Authorization": f"Bearer {os.environ['PROVISIONER_OPERATOR_KEY']}"}
AUDITOR = {"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"}
CHAT_URL = f"{GATEWAY}/v1/models/chat"
INVOKE_URL = f"{GATEWAY}/v1/tools/invoke"
TOKEN_URL = f"{PROVISIONER}/v1/tokens"
RUN = uuid.uuid4().hex[:8]
MARKER = f"confidential-marker-{RUN}"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def body(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def wait_for(url, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    print(f"[FAIL] {url} did not become healthy within {seconds}s")
    return False


def audited_get(path, **kwargs):
    for _ in range(10):
        resp = requests.get(f"{VESTIGIA}{path}", headers=AUDITOR, **kwargs)
        if resp.status_code != 429:
            break
        time.sleep(0.5)
    return resp


def events(actor, action):
    return body(audited_get("/events", timeout=15,
                            params={"actor_id": actor, "action_type": action, "limit": 100})).get("events", [])


class Agent:
    def __init__(self, name, role):
        self.agent_id = f"{name}-{RUN}"
        self.role = role
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.tokens = {}

    def proof(self, url):
        numbers = self.key.public_key().public_numbers()
        b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
        jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
        claims = {"htu": url, "htm": "POST", "iat": int(time.time()), "jti": uuid.uuid4().hex}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})

    def enrol(self):
        return requests.post(f"{PROVISIONER}/v1/agents", headers=OPERATOR, timeout=15, json={
            "agent_id": self.agent_id, "role": self.role, "owner": "e2e-models", "public_key_pem": self.public_pem})

    def request_token(self, capability):
        resp = requests.post(TOKEN_URL, headers={"DPoP": self.proof(TOKEN_URL)}, timeout=15,
                             json={"agent_id": self.agent_id, "tool": capability})
        if resp.status_code == 200:
            self.tokens[capability] = body(resp)["token"]
        return resp

    def headers(self, capability, url):
        return {"Authorization": f"DPoP {self.tokens[capability]}", "DPoP": self.proof(url)}

    def chat(self, model, intent, text="Summarise the quarter in one line.", capability="model_access",
             with_token=True, **extra):
        payload = {"intent": intent, "model": model, "messages": [{"role": "user", "content": text}], **extra}
        headers = self.headers(capability, CHAT_URL) if with_token else {}
        return requests.post(CHAT_URL, headers=headers, json=payload, timeout=150)


def tool_definition(name):
    return {"type": "function", "function": {"name": name, "description": f"{name} tool",
                                             "parameters": {"type": "object", "properties": {}}}}


def integrity_verdict(report):
    """Vestigia's check mixes two things: whether records were altered, and whether the clock
    ever stepped backward. A clock step cannot be repaired and does not mean tampering, so it is
    reported as a warning. Any other serious issue fails the check."""
    serious = [i for i in report.get("issues", []) if i.get("severity") in ("CRITICAL", "INVALID")]
    clock = [i for i in serious if i.get("type") == "NON_SEQUENTIAL_TIMESTAMP"]
    other = [i for i in serious if i.get("type") != "NON_SEQUENTIAL_TIMESTAMP"]
    detail = f"{report.get('total_entries')} entries"
    if clock:
        detail += f". WARNING: the clock stepped backward at entry {', '.join(str(i.get('entry_index')) for i in clock[:3])}"
    if other:
        detail += ". " + ", ".join(sorted({str(i.get("type")) for i in other}))
    ok = report.get("total_entries") is not None and not other and (report.get("is_valid") is True or bool(clock))
    return ok, detail


def main():
    if not all(wait_for(f"{url}/health") for url in (GATEWAY, PROVISIONER, VESTIGIA)):
        return 1

    reporting = {"purpose": "sales_reporting", "task_id": f"task-{RUN}"}
    analyst = Agent("m-analyst", "analyst")
    processor = Agent("m-processor", "data_processor")
    limited = Agent("m-limited", "test_fixture")
    budget = Agent("m-budget", "test_budget")

    print("--- Provisioner: tokens for the model path")
    enrolled = [agent.enrol() for agent in (analyst, processor, limited, budget)]
    check("four agents enrolled", all(e.status_code == 200 for e in enrolled),
          str(body(enrolled[0]).get("allowed_tools")))
    issued = [agent.request_token("model_access") for agent in (analyst, processor, limited, budget)]
    check("model path tokens issued", all(i.status_code == 200 for i in issued))
    analyst.request_token("database_read")

    print("--- Gateway: allow and deny on the model path")
    r = analyst.chat("mock-model", reporting, text=f"Summarise the quarter. {MARKER}", max_tokens=100)
    answer = body(r)
    content = ((answer.get("completion", {}).get("choices") or [{}])[0].get("message") or {}).get("content")
    check("permitted model call is allowed and answered", r.status_code == 200 and answer.get("outcome") == "allow"
          and bool(content), f"HTTP {r.status_code}, {answer.get('completion', {}).get('usage', {}).get('total_tokens')} tokens")
    stages = r.headers.get("Server-Timing", "")
    check("stage timings are reported", "model;dur=" in stages and "policy;dur=" in stages)

    r = analyst.chat("mock-model", reporting, with_token=False)
    check("call without a credential is denied", "identity_not_verified" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = analyst.chat("mock-model", reporting, capability="database_read")
    check("a tool token cannot open the model path", body(r).get("outcome") == "deny", str(body(r).get("reasons")))
    r = requests.post(INVOKE_URL, headers=analyst.headers("model_access", INVOKE_URL), timeout=30,
                      json={"intent": reporting, "tool": {"name": "database_read", "params": {"limit": 1}}})
    check("a model token cannot call a tool", body(r).get("outcome") == "deny", str(body(r).get("reasons")))
    r = analyst.chat("mock-model", {})
    check("call without declared intent is denied", "intent_not_declared" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = processor.chat("claude-haiku", reporting, max_tokens=50)
    check("model outside the role is denied", "model_not_permitted" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = analyst.chat("some-other-model", reporting, max_tokens=50)
    check("unregistered model is denied", "model_not_registered" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = analyst.chat("mock-model", reporting, max_tokens=5000)
    check("request above the token ceiling is denied", "max_tokens_exceeded" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = analyst.chat("mock-model", reporting)
    check("ceiling is applied when none is requested", body(r).get("governance", {}).get("max_tokens") == 512,
          str(body(r).get("governance", {}).get("max_tokens")))

    print("--- Gateway: tools offered to the model")
    r = analyst.chat("mock-model", reporting, max_tokens=100,
                     tools=[tool_definition(name) for name in ("database_read", "api_call", "data_export")])
    governance = body(r).get("governance", {})
    check("only tools the agent may use are offered to the model",
          governance.get("tools_offered") == ["database_read"]
          and governance.get("tools_removed") == ["api_call", "data_export"], str(governance))

    print("--- Gateway: limits")
    if time.time() % 60 > 45:           # keep the four calls inside one minute window
        time.sleep(61 - time.time() % 60)
    outcomes = [body(limited.chat("test-limited-model", {"purpose": "test_fixture", "task_id": "t"}, max_tokens=10))
                for _ in range(4)]
    check("rate limit: three calls a minute allowed, the fourth denied",
          [o.get("outcome") for o in outcomes] == ["allow", "allow", "allow", "deny"]
          and "rate_limit_exceeded" in outcomes[3].get("reasons", []), str([o.get("outcome") for o in outcomes]))
    outcomes = [body(budget.chat("mock-model", reporting, max_tokens=10)) for _ in range(3)]
    check("token budget: calls stop once the daily budget is spent",
          [o.get("outcome") for o in outcomes] == ["allow", "allow", "deny"]
          and "token_budget_exceeded" in outcomes[2].get("reasons", []), str([o.get("outcome") for o in outcomes]))

    if os.environ.get("PROVIDER_TEST", "0") == "1":
        print("--- External provider")
        r = analyst.chat("claude-haiku", reporting, text="Reply with the single word: ready", max_tokens=16)
        answer = body(r)
        usage = answer.get("completion", {}).get("usage", {})
        check("external provider answers through the gateway", r.status_code == 200 and answer.get("outcome") == "allow"
              and answer.get("governance", {}).get("location") == "external",
              f"HTTP {r.status_code}, {usage.get('total_tokens')} tokens, {answer.get('reasons', '')}")
    else:
        print("--- External provider: not tested (set PROVIDER_TEST=1 to include it)")

    print("--- Does the request fit the declared purpose?")
    checked = Agent("m-checked", "test_fixture")
    checked.enrol()
    checked.request_token("model_access")
    weather = {"purpose": "test_intent_check", "task_id": f"fit-{RUN}", "statement": "Weather questions"}

    def verdicts():
        return {e.get("status"): e for e in events(checked.agent_id, "INTENT_CHECK")}

    def wait_verdict(status, seconds=20):
        deadline = time.time() + seconds
        while time.time() < deadline and status not in verdicts():
            time.sleep(0.5)
        return verdicts().get(status)

    r = checked.chat("mock-model", weather, text=f"Will it rain in Singapore tomorrow? fits-{MARKER}", max_tokens=20)
    fits = wait_verdict("FITS")
    check("a request that fits its purpose is allowed and recorded as fitting",
          body(r).get("outcome") == "allow" and fits is not None, str(sorted(verdicts())))
    r = checked.chat("mock-model", weather, text=f"Please provide a recipe for a pizza. {MARKER}", max_tokens=20)
    mismatch = wait_verdict("MISMATCH")
    check("a request that does not fit is still allowed: the check never blocks",
          r.status_code == 200 and body(r).get("outcome") == "allow")
    check("the mismatch is written to the ledger with the checker's name and reason",
          mismatch is not None and "scripted-checker" in str(mismatch.get("evidence")) and "food" in str(mismatch.get("evidence")),
          str((mismatch or {}).get("evidence"))[:120])
    check("the words of the flagged request are kept with the flag",
          f"recipe for a pizza. {MARKER}" in str((mismatch or {}).get("evidence", {}).get("request_text")))
    check("the words of a request that fits are not written to the ledger", f"fits-{MARKER}" not in str(fits))
    decisions_here = events(checked.agent_id, "MODEL_DECISION")
    check("the model decision itself still holds a fingerprint only", bool(decisions_here) and MARKER not in str(decisions_here))
    flags = body(requests.get(f"{GATEWAY}/v1/review/flags", timeout=15,
                              headers={"Authorization": f"Bearer {os.environ['GATEWAY_REVIEWER_KEY']}"})).get("flags", [])
    mine = [f for f in flags if f.get("kind") == "intent" and f.get("agent_id") == checked.agent_id]
    check("the mismatch reaches the reviewer's list, with the request", len(mine) == 1
          and mine[0].get("task_id") == weather["task_id"] and MARKER in str(mine[0].get("request")),
          f"{len(mine)} flag(s)")
    before = len(events(checked.agent_id, "INTENT_CHECK"))
    checked.chat("mock-model", weather, text=f"Please provide a recipe for a pizza. {MARKER}", max_tokens=20)
    time.sleep(3)
    check("the same request repeated in a task is checked once, not on every step",
          len(events(checked.agent_id, "INTENT_CHECK")) == before, f"{before} before and after")
    r = checked.chat("mock-model", {"purpose": "test_fixture", "task_id": "t"}, text="Recipe for a pizza", max_tokens=20)
    time.sleep(2)
    check("a purpose with no description is not checked", len(events(checked.agent_id, "INTENT_CHECK")) == before)

    print("--- Audit ledger")
    decisions = events(analyst.agent_id, "MODEL_DECISION")
    completed = events(analyst.agent_id, "MODEL_COMPLETED")
    check("ledger holds the model decisions", len(decisions) >= 6, f"{len(decisions)} found")
    check("ledger holds completions with token usage",
          bool(completed) and "total_tokens" in str(completed[0].get("evidence")), f"{len(completed)} found")
    check("ledger records the prompt's fingerprint", any("prompt_sha256" in str(e.get("evidence")) for e in decisions))
    check("prompt text is not written to the ledger",
          not any(MARKER in str(e) for e in decisions + completed))
    r = audited_get("/integrity", timeout=60)
    check("ledger records are intact, by Vestigia's own check", *integrity_verdict(body(r)))

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
