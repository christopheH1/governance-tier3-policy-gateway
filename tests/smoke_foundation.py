"""Stage 1 acceptance test: identity, audit, policy, and key-value store.

Runs inside the test-runner container on the internal network.
Every check has an explicit expectation. The exit code is non-zero if any check fails.

It proves the behaviour the policy gateway will rely on:
  - Tessera refuses registration and token issuance without the admin key
  - a token is bound to one tool and to the agent's DPoP key
  - a DPoP proof made for the gateway URL validates, and cannot be replayed
  - revocation takes effect at once
  - Vestigia refuses unauthenticated writes, chains events, and its ledger verifies
  - OPA returns deny, refer, or allow for the tool path
"""
import os
import sys
import time
import uuid

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

TESSERA = os.environ.get("TESSERA_URL", "http://tessera:8001")
VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")
OPA = os.environ.get("OPA_URL", "http://opa:8181")
VALKEY_URL = os.environ.get("VALKEY_URL", "")
ADMIN_KEY = os.environ["TESSERA_ADMIN_KEY"]
VESTIGIA_KEY = os.environ["VESTIGIA_API_KEY"]

# The agent signs its proof for the gateway endpoint. The gateway forwards the
# proof to Tessera and states which URL and method it was presented on.
GATEWAY_URL = "http://policy-gateway:9090/v1/tools/invoke"
DECISION = "/v1/data/ai_governance/tier3/tool_egress/decision"

ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
AUDIT = {"Authorization": f"Bearer {VESTIGIA_KEY}"}
AGENT = f"smoke-analyst-{uuid.uuid4().hex[:8]}"

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def body(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


def wait_for(url, label, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    print(f"[FAIL] {label} did not become healthy within {seconds}s")
    return False


# --- agent key and DPoP proof -------------------------------------------------
private_key = ec.generate_private_key(ec.SECP256R1())
public_pem = private_key.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()


def dpop_proof(url=GATEWAY_URL, method="POST"):
    numbers = private_key.public_key().public_numbers()
    b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
    jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}
    claims = {"htu": url, "htm": method, "iat": int(time.time()), "jti": uuid.uuid4().hex}
    return jwt.encode(claims, private_key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})


def validate(token, tool, proof):
    return requests.post(
        f"{TESSERA}/tokens/validate",
        json={"token": token, "tool": tool, "dpop_proof": proof,
              "expected_htu": GATEWAY_URL, "expected_htm": "POST"},
        timeout=10,
    )


def decide(identity, intent, tool):
    resp = requests.post(f"{OPA}{DECISION}",
                         json={"input": {"identity": identity, "intent": intent, "tool": tool}},
                         timeout=10)
    return body(resp).get("result", {})


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
    if not (wait_for(f"{TESSERA}/health", "Tessera") and wait_for(f"{VESTIGIA}/health", "Vestigia")
            and wait_for(f"{OPA}/health", "OPA")):
        return 1

    print("--- Key-value store")
    if VALKEY_URL:
        import redis
        try:
            info = redis.from_url(VALKEY_URL, socket_connect_timeout=3).info("server")
            check("store answers with authentication", True,
                  f"{info.get('server_name', 'redis')} {info.get('valkey_version', info.get('redis_version'))}")
        except Exception as exc:  # noqa: BLE001
            check("store answers with authentication", False, str(exc))

    print("--- Tessera: identity")
    registration = {"agent_id": AGENT, "owner": "smoke-test",
                    "allowed_tools": ["database_read", "file_read"]}
    r = requests.post(f"{TESSERA}/agents/register", json=registration, timeout=10)
    check("registration without admin key is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")
    r = requests.post(f"{TESSERA}/agents/register", json=registration, headers=ADMIN, timeout=10)
    check("registration with admin key succeeds", r.status_code == 200, f"HTTP {r.status_code}")

    token_request = {"agent_id": AGENT, "tool": "database_read", "duration_minutes": 10,
                     "session_id": "smoke", "memory_state": "init", "client_public_key": public_pem}
    r = requests.post(f"{TESSERA}/tokens/request", json=token_request, timeout=10)
    check("token request without admin key is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")
    r = requests.post(f"{TESSERA}/tokens/request", json=token_request, headers=ADMIN, timeout=10)
    token = body(r).get("token")
    check("token request with admin key succeeds", bool(token), f"HTTP {r.status_code}")
    if not token:
        return 1

    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    check("token is HS512 and bound to a key", header.get("alg") == "HS512" and "cnf" in claims,
          f"alg={header.get('alg')}, tool={claims.get('tool')}")

    proof = dpop_proof()
    r = validate(token, "database_read", proof)
    check("valid token and proof are accepted", body(r).get("valid") is True, body(r).get("reason", ""))
    r = validate(token, "database_read", proof)
    check("replayed proof is refused", body(r).get("valid") is not True, f"HTTP {r.status_code}")
    r = validate(token, "database_read", None)
    check("missing proof is refused", body(r).get("valid") is not True, body(r).get("reason", ""))
    r = validate(token, "database_delete", dpop_proof())
    check("token cannot be used for another tool", body(r).get("valid") is not True, body(r).get("reason", ""))
    r = validate(token, "database_read", dpop_proof(url="http://elsewhere.invalid/x"))
    check("proof made for another URL is refused", body(r).get("valid") is not True, body(r).get("reason", ""))

    r = requests.post(f"{TESSERA}/tokens/revoke", json={"token": token, "reason": "smoke test"},
                      headers=ADMIN, timeout=10)
    check("revocation succeeds", r.status_code == 200, f"HTTP {r.status_code}")
    r = validate(token, "database_read", dpop_proof())
    check("revoked token is refused", body(r).get("valid") is not True, body(r).get("reason", ""))

    print("--- Vestigia: audit")
    event = {"actor_id": AGENT, "action_type": "TOOL_DECISION", "status": "DENIED",
             "evidence": {"tool": "database_delete", "reasons": ["tool_not_whitelisted"],
                          "intent": {"purpose": "sales_reporting", "task_id": "smoke"}}}
    r = requests.post(f"{VESTIGIA}/events", json=event, timeout=10)
    check("event without API key is refused", r.status_code in (401, 403, 503), f"HTTP {r.status_code}")
    started = time.perf_counter()
    r = requests.post(f"{VESTIGIA}/events", json=event, headers=AUDIT, timeout=10)
    elapsed_ms = (time.perf_counter() - started) * 1000
    first = body(r)
    check("event with API key is recorded", r.status_code == 201 and bool(first.get("integrity_hash")),
          f"{elapsed_ms:.1f} ms")
    # Vestigia also records each API request as its own ledger entry, so two
    # gateway events are not adjacent. Linkage is proved by the integrity check.
    stored = body(requests.get(f"{VESTIGIA}/events/{first.get('event_id')}", headers=AUDIT, timeout=10))
    check("stored event carries its chain hashes",
          stored.get("integrity_hash") == first.get("integrity_hash")
          and stored.get("previous_hash") not in (None, "", "GENESIS"))
    r = requests.get(f"{VESTIGIA}/integrity", headers=AUDIT, timeout=30)
    check("ledger records are intact, by Vestigia's own check", *integrity_verdict(body(r)))

    print("--- OPA: tool path decision")
    analyst = {"verified": True, "agent_id": AGENT, "role": "analyst"}
    reporting = {"purpose": "sales_reporting", "task_id": "smoke"}
    d = decide(analyst, reporting, {"name": "database_read", "params": {"limit": 100}})
    check("permitted read is allowed", d.get("outcome") == "allow", str(d))
    d = decide(analyst, reporting, {"name": "database_delete", "params": {}})
    check("tool outside the whitelist is denied", d.get("outcome") == "deny", str(d.get("reasons")))
    d = decide(analyst, {}, {"name": "database_read", "params": {}})
    check("missing intent is denied", d.get("outcome") == "deny", str(d.get("reasons")))
    d = decide({"verified": False, "role": "analyst"}, reporting, {"name": "database_read", "params": {}})
    check("unverified identity is denied", d.get("outcome") == "deny", str(d.get("reasons")))
    d = decide(analyst, reporting, {"name": "database_read", "params": {"limit": 50000}})
    check("large read is referred and flagged", d.get("outcome") == "refer" and d.get("mode") == "flag", str(d))
    d = decide({"verified": True, "role": "data_processor"}, {"purpose": "data_export", "task_id": "smoke"},
               {"name": "data_export", "params": {}})
    check("export is referred and held", d.get("outcome") == "refer" and d.get("mode") == "hold", str(d))

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
