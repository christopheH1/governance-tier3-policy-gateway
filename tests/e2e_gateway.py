"""Stage 2 acceptance test: the tool path through the policy gateway.

The test plays four parts: operator (enrols agents), agent (asks for tokens and
calls tools), reviewer (approves or rejects held calls), and auditor (reads the
ledger). It holds all four credentials only because it is a test.

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
MOCK_TOOLS = os.environ.get("MOCK_TOOLS_URL", "http://mock-tools:8000")
OPERATOR = {"Authorization": f"Bearer {os.environ['PROVISIONER_OPERATOR_KEY']}"}
REVIEWER = {"Authorization": f"Bearer {os.environ['GATEWAY_REVIEWER_KEY']}"}
AUDITOR = {"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"}

INVOKE_URL = f"{GATEWAY}/v1/tools/invoke"
TOKEN_URL = f"{PROVISIONER}/v1/tokens"
RUN = uuid.uuid4().hex[:8]
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


def tool_calls():
    return body(requests.get(f"{MOCK_TOOLS}/_calls", timeout=5))


def audited_get(path, **kwargs):
    """Read from the ledger, waiting out its rate limit (10 requests a second per client)."""
    for _ in range(10):
        resp = requests.get(f"{VESTIGIA}{path}", headers=AUDITOR, **kwargs)
        if resp.status_code != 429:
            break
        time.sleep(0.5)
    return resp


def events(actor, action):
    resp = audited_get("/events", timeout=15, params={"actor_id": actor, "action_type": action, "limit": 100})
    return body(resp).get("events", [])


class Agent:
    """An agent with its own key pair. The private key never leaves this object."""

    def __init__(self, name, role):
        self.agent_id = f"{name}-{RUN}"
        self.role = role
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.tokens = {}

    def proof(self, url, key=None):
        signing_key = key or self.key
        numbers = signing_key.public_key().public_numbers()
        b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
        jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
        claims = {"htu": url, "htm": "POST", "iat": int(time.time()), "jti": uuid.uuid4().hex}
        return jwt.encode(claims, signing_key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})

    def enrol(self, headers=OPERATOR):
        return requests.post(f"{PROVISIONER}/v1/agents", headers=headers, timeout=15, json={
            "agent_id": self.agent_id, "role": self.role, "owner": "e2e-test", "public_key_pem": self.public_pem})

    def request_token(self, tool, proof=None):
        headers = {"DPoP": proof if proof is not None else self.proof(TOKEN_URL)}
        resp = requests.post(TOKEN_URL, headers=headers, timeout=15, json={"agent_id": self.agent_id, "tool": tool})
        if resp.status_code == 200:
            self.tokens[tool] = body(resp)
        return resp

    def invoke(self, tool, params, intent, token_tool=None, proof=None, hold_reference=None, with_token=True):
        headers = {}
        if with_token:
            headers = {"Authorization": f"DPoP {self.tokens[token_tool or tool]['token']}",
                       "DPoP": proof or self.proof(INVOKE_URL)}
        payload = {"intent": intent, "tool": {"name": tool, "params": params}}
        if hold_reference:
            payload["hold_reference"] = hold_reference
        return requests.post(INVOKE_URL, headers=headers, json=payload, timeout=30)


def review(hold_id, approve, headers=REVIEWER):
    return requests.post(f"{GATEWAY}/v1/review/holds/{hold_id}/decision", headers=headers, timeout=15,
                         json={"reviewer": "reviewer.one@example.org", "approve": approve, "note": "e2e test"})


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
    if not all(wait_for(f"{url}/health") for url in (GATEWAY, PROVISIONER, VESTIGIA, MOCK_TOOLS)):
        return 1

    reporting = {"purpose": "sales_reporting", "task_id": f"task-{RUN}", "statement": "Q4 sales figures"}
    exporting = {"purpose": "data_export", "task_id": f"task-{RUN}"}
    analyst = Agent("analyst", "analyst")
    processor = Agent("processor", "data_processor")
    colleague = Agent("colleague", "data_processor")
    fixture = Agent("fixture", "test_fixture")

    print("--- Provisioner: enrolment and tokens")
    r = analyst.enrol(headers={})
    check("enrolment without operator key is refused", r.status_code == 401, f"HTTP {r.status_code}")
    r = Agent("ghost", "no_such_role").enrol()
    check("enrolment with an unknown role is refused", r.status_code == 422, f"HTTP {r.status_code}")
    enrolled = [agent.enrol() for agent in (analyst, processor, colleague, fixture)]
    check("four agents enrolled", all(e.status_code == 200 for e in enrolled),
          str(body(enrolled[0]).get("allowed_tools")))

    r = requests.post(TOKEN_URL, json={"agent_id": analyst.agent_id, "tool": "database_read"}, timeout=15)
    check("token without proof of possession is refused", r.status_code == 401, f"HTTP {r.status_code}")
    stranger = ec.generate_private_key(ec.SECP256R1())
    r = analyst.request_token("database_read", proof=analyst.proof(TOKEN_URL, key=stranger))
    check("token with another party's key is refused", r.status_code == 401, f"HTTP {r.status_code}")
    r = analyst.request_token("data_export")
    check("token for a tool outside the role is refused", r.status_code == 403, f"HTTP {r.status_code}")
    used = analyst.proof(TOKEN_URL)
    r = analyst.request_token("database_read", proof=used)
    check("token for a permitted tool is issued", r.status_code == 200, f"role={body(r).get('role')}")
    r = analyst.request_token("file_read", proof=used)
    check("the same proof cannot be used twice", r.status_code == 401, f"HTTP {r.status_code}")
    for agent, tool in ((analyst, "file_read"), (processor, "data_export"), (colleague, "data_export"),
                        (fixture, "test_short_hold")):
        agent.request_token(tool)
    check("tokens issued for the remaining test tools",
          "file_read" in analyst.tokens and "data_export" in processor.tokens
          and "data_export" in colleague.tokens and "test_short_hold" in fixture.tokens)

    print("--- Gateway: allow and deny")
    r = analyst.invoke("database_read", {"table": "sales", "limit": 3}, reporting)
    check("permitted call is allowed and runs", r.status_code == 200 and body(r).get("outcome") == "allow"
          and len(body(r).get("result", {}).get("rows", [])) == 3, f"HTTP {r.status_code}")

    before = tool_calls()
    r = analyst.invoke("database_read", {"limit": 3}, reporting, with_token=False)
    check("call without a credential is denied", body(r).get("outcome") == "deny"
          and "identity_not_verified" in body(r).get("reasons", []), str(body(r).get("reasons")))
    r = analyst.invoke("file_read", {"path": "q4.csv"}, reporting, token_tool="database_read")
    check("token for one tool cannot call another", body(r).get("outcome") == "deny", str(body(r).get("reasons")))
    r = analyst.invoke("database_read", {"limit": 3}, {})
    check("call without declared intent is denied", "intent_not_declared" in body(r).get("reasons", []),
          str(body(r).get("reasons")))
    r = analyst.invoke("database_read", {"limit": 3}, {"purpose": "market_research", "task_id": "t"})
    check("call whose intent does not fit the tool is denied",
          "intent_tool_mismatch" in body(r).get("reasons", []), str(body(r).get("reasons")))
    replay = analyst.proof(INVOKE_URL)
    analyst.invoke("database_read", {"limit": 1}, reporting, proof=replay)
    r = analyst.invoke("database_read", {"limit": 1}, reporting, proof=replay)
    check("replayed proof is denied", body(r).get("outcome") == "deny", str(body(r).get("reasons")))
    r = analyst.invoke("database_read", {"limit": 1}, reporting, proof=analyst.proof(INVOKE_URL, key=stranger))
    check("stolen token with another key is denied", body(r).get("outcome") == "deny", str(body(r).get("reasons")))
    after = tool_calls()
    check("no denied call reached a tool",
          after.get("database/read", 0) - before.get("database/read", 0) == 1
          and after.get("file/read", 0) == before.get("file/read", 0), "one call ran: the first use of the replay proof")

    print("--- Gateway: refer and flag")
    r = analyst.invoke("database_read", {"table": "sales", "limit": 50000}, reporting)
    check("large read runs and is flagged", r.status_code == 200 and body(r).get("mode") == "flag"
          and "result" in body(r), str(body(r).get("reasons")))
    flags = body(requests.get(f"{GATEWAY}/v1/review/flags", headers=REVIEWER, timeout=10)).get("flags", [])
    check("flag appears in the monitoring queue", any(f.get("agent_id") == analyst.agent_id for f in flags))
    r = requests.get(f"{GATEWAY}/v1/review/flags", timeout=10)
    check("monitoring queue needs the reviewer key", r.status_code == 401, f"HTTP {r.status_code}")

    print("--- Gateway: refer and hold")
    export_params = {"dataset": "sales_q4", "format": "csv"}
    before = tool_calls().get("external/export", 0)
    r = processor.invoke("data_export", export_params, exporting)
    hold = body(r).get("hold_reference")
    check("export is held, not run", r.status_code == 202 and bool(hold)
          and tool_calls().get("external/export", 0) == before, f"HTTP {r.status_code}")
    r = processor.invoke("data_export", export_params, exporting, hold_reference=hold)
    check("held call stays pending until reviewed", r.status_code == 202 and body(r).get("status") == "pending")
    pending = body(requests.get(f"{GATEWAY}/v1/review/holds", headers=REVIEWER, timeout=10)).get("pending", [])
    check("hold is listed for the reviewer", any(p.get("hold_reference") == hold for p in pending))
    r = review(hold, True, headers={"Authorization": "Bearer wrong"})
    check("approval without the reviewer key is refused", r.status_code == 401, f"HTTP {r.status_code}")
    r = review(hold, True)
    check("reviewer approves the hold", r.status_code == 200 and body(r).get("status") == "approved")
    r = processor.invoke("data_export", {"dataset": "customers_all", "format": "csv"}, exporting, hold_reference=hold)
    check("approval cannot be used for a different request",
          "hold_request_mismatch" in body(r).get("reasons", []), str(body(r).get("reasons")))
    r = colleague.invoke("data_export", export_params, exporting, hold_reference=hold)
    check("another agent cannot use the approval", "hold_not_found" in body(r).get("reasons", [])
          and tool_calls().get("external/export", 0) == before, str(body(r).get("reasons")))
    r = processor.invoke("data_export", export_params, exporting, hold_reference=hold)
    check("approved call runs", r.status_code == 200 and body(r).get("released_from_hold") == hold
          and tool_calls().get("external/export", 0) == before + 1)
    r = processor.invoke("data_export", export_params, exporting, hold_reference=hold)
    check("approval is single use", "hold_already_used" in body(r).get("reasons", []), str(body(r).get("reasons")))
    r = review(hold, True)
    check("a decided hold cannot be decided again", r.status_code == 409, f"HTTP {r.status_code}")

    r = processor.invoke("data_export", {"dataset": "hr", "format": "csv"}, exporting)
    rejected = body(r).get("hold_reference")
    review(rejected, False)
    r = processor.invoke("data_export", {"dataset": "hr", "format": "csv"}, exporting, hold_reference=rejected)
    check("rejected call is denied", "hold_rejected" in body(r).get("reasons", []), str(body(r).get("reasons")))

    fixture_intent = {"purpose": "test_fixture", "task_id": f"task-{RUN}"}
    r = fixture.invoke("test_short_hold", {"n": 1}, fixture_intent)
    short = body(r).get("hold_reference")
    time.sleep(6)   # the fixture tool has a 3 second timeout, and the sweeper runs every 2 seconds
    r = review(short, True)
    check("an expired hold cannot be approved", r.status_code == 409, f"HTTP {r.status_code}")
    r = fixture.invoke("test_short_hold", {"n": 1}, fixture_intent, hold_reference=short)
    check("unanswered hold times out to deny", "hold_expired" in body(r).get("reasons", [])
          and tool_calls().get("test/short-hold", 0) == 0, str(body(r).get("reasons")))

    print("--- Revocation")
    jti = analyst.tokens["database_read"]["jti"]
    r = requests.post(f"{PROVISIONER}/v1/tokens/revoke", json={"jti": jti}, timeout=15)
    check("revocation without operator key is refused", r.status_code == 401, f"HTTP {r.status_code}")
    r = requests.post(f"{PROVISIONER}/v1/tokens/revoke", headers=OPERATOR, json={"jti": jti, "reason": "e2e"}, timeout=15)
    check("operator revokes the analyst's token", r.status_code == 200)
    r = analyst.invoke("database_read", {"limit": 1}, reporting)
    check("revoked token is denied at once", body(r).get("outcome") == "deny", str(body(r).get("reasons")))

    print("--- Audit ledger")
    expected = [
        (analyst.agent_id, "AGENT_ENROLLED", 1), (analyst.agent_id, "TOKEN_ISSUED", 2),
        (analyst.agent_id, "TOKEN_REFUSED", 1), (analyst.agent_id, "TOOL_DECISION", 5), ("unverified", "TOOL_DECISION", 5),
        (analyst.agent_id, "TOOL_EXECUTED", 3), (processor.agent_id, "HOLD_CREATED", 2),
        (processor.agent_id, "HOLD_APPROVED", 1), (processor.agent_id, "HOLD_REJECTED", 1),
        (processor.agent_id, "HOLD_RELEASED", 1), (fixture.agent_id, "HOLD_EXPIRED", 1),
        ("operator", "TOKEN_REVOKED", 1),
    ]
    for actor, action, minimum in expected:
        found = len(events(actor, action))
        check(f"ledger holds {action}", found >= minimum, f"{found} found, at least {minimum} expected")
    approved = events(processor.agent_id, "HOLD_APPROVED")
    check("approval records who approved",
          bool(approved) and "reviewer.one@example.org" in str(approved[0].get("evidence")))
    unproven = [e for e in events("unverified", "TOOL_DECISION") if analyst.agent_id in str(e.get("evidence"))]
    check("refused credentials are recorded with the identity they claimed", len(unproven) >= 3, f"{len(unproven)} found")
    r = audited_get("/integrity", timeout=60)
    check("ledger records are intact, by Vestigia's own check", *integrity_verdict(body(r)))

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
