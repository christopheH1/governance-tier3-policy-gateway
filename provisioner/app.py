"""Provisioning service: the only holder of the Tessera admin key.

It enrols agents and issues their tokens, so the policy gateway never needs to.
  - Operators enrol an agent with a role and the agent's public key.
  - An agent proves it holds the matching private key and asks for a token for one tool.
  - A token is issued only for tools the agent's role permits, as defined in the
    same registry that OPA uses. A role that may use models can also receive a
    token for the model path.
Every action is written to the ledger. If it cannot be recorded, it does not stand.
"""
import hmac
import logging
import os
import time
from typing import Optional

import httpx
import jwt
import redis
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("provisioner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TESSERA_URL = os.environ["TESSERA_URL"].rstrip("/")
TESSERA_ADMIN_KEY = os.environ["TESSERA_ADMIN_KEY"]
OPA_URL = os.environ["OPA_URL"].rstrip("/")
VESTIGIA_URL = os.environ["VESTIGIA_URL"].rstrip("/")
VESTIGIA_API_KEY = os.environ["VESTIGIA_API_KEY"]
VALKEY_URL = os.environ["VALKEY_URL"]
OPERATOR_KEY = os.environ["PROVISIONER_OPERATOR_KEY"]
PUBLIC_URL = os.environ.get("PROVISIONER_PUBLIC_URL", "http://provisioner:9080").rstrip("/")
TOKEN_URL = f"{PUBLIC_URL}/v1/tokens"
TOKEN_TTL_MINUTES = int(os.environ.get("TOKEN_TTL_MINUTES", "15"))
PROOF_MAX_AGE = 60

ADMIN = {"Authorization": f"Bearer {TESSERA_ADMIN_KEY}"}
http = httpx.Client(timeout=httpx.Timeout(5.0, connect=2.0))
store = redis.from_url(VALKEY_URL, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

app = FastAPI(title="Provisioner", docs_url=None, redoc_url=None, openapi_url=None)


class Enrolment(BaseModel):
    agent_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    role: str = Field(min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=128)
    public_key_pem: str


class TokenRequest(BaseModel):
    agent_id: str
    tool: str


class Revocation(BaseModel):
    jti: str
    reason: str = "operator revocation"


def refuse(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def is_operator(authorization: Optional[str]) -> bool:
    scheme, _, key = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(key, OPERATOR_KEY)


def audit(actor: str, action: str, status: str, evidence: dict) -> None:
    """Write one event to the ledger, retrying briefly if Vestigia's rate limit answers 429."""
    resp = None
    for delay in (0.15, 0.3, 0.6, None):
        resp = http.post(f"{VESTIGIA_URL}/events", headers={"Authorization": f"Bearer {VESTIGIA_API_KEY}"},
                         json={"actor_id": actor, "action_type": action, "status": status, "evidence": evidence})
        if resp.status_code != 429 or delay is None:
            break
        time.sleep(delay)
    if resp.status_code != 201:
        raise RuntimeError(f"audit write refused: HTTP {resp.status_code}")


MODEL_ACCESS = "model_access"   # the capability a token must carry to use the gateway's model path


def role_tools(role: str) -> Optional[list]:
    """What a role may be issued tokens for, read from the registry that OPA serves:
    its tools, plus the model path if the role is permitted any model."""
    resp = http.get(f"{OPA_URL}/v1/data/tier3/roles/{role}")
    entry = resp.json().get("result") if resp.status_code == 200 else None
    if not isinstance(entry, dict) or not isinstance(entry.get("tools"), list):
        return None
    return entry["tools"] + ([MODEL_ACCESS] if entry.get("models") else [])


def agent_key(agent_id: str) -> str:
    return f"prov:agent:{agent_id}"


@app.post("/v1/agents")
def enrol(body: Enrolment, authorization: Optional[str] = Header(None)):
    if not is_operator(authorization):
        return refuse(401, "operator credential required")
    try:
        key = serialization.load_pem_public_key(body.public_key_pem.encode())
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            return refuse(422, "public key must be EC P-256")
        tools = role_tools(body.role)
        if tools is None:
            return refuse(422, f"unknown role '{body.role}'")
        resp = http.post(f"{TESSERA_URL}/agents/register", headers=ADMIN, json={
            "agent_id": body.agent_id, "owner": body.owner,
            "allowed_tools": tools, "allowed_roles": [body.role]})
        if resp.status_code != 200:
            return refuse(502, f"identity service refused registration: HTTP {resp.status_code}")
        audit(body.agent_id, "AGENT_ENROLLED", "SUCCESS",
              {"role": body.role, "owner": body.owner, "allowed_tools": tools})
        store.hset(agent_key(body.agent_id), mapping={
            "role": body.role, "owner": body.owner, "public_key_pem": body.public_key_pem})
        return {"agent_id": body.agent_id, "role": body.role, "allowed_tools": tools}
    except ValueError:
        return refuse(422, "public key is not valid PEM")
    except Exception:  # noqa: BLE001
        log.exception("enrolment failed agent=%s", body.agent_id)
        return refuse(503, "enrolment could not be completed")


@app.post("/v1/tokens")
def issue_token(body: TokenRequest, dpop: Optional[str] = Header(None, alias="DPoP")):
    try:
        agent = store.hgetall(agent_key(body.agent_id))
        if not agent or not dpop:
            return refuse(401, "enrolled agent and proof of possession required")
        # The agent proves it holds the private key that was enrolled for it.
        try:
            proof = jwt.decode(dpop, agent["public_key_pem"], algorithms=["ES256"],
                               options={"require": ["htu", "htm", "iat", "jti"], "verify_aud": False})
        except jwt.PyJWTError:
            return refuse(401, "proof of possession is not valid")
        if proof["htu"] != TOKEN_URL or proof["htm"] != "POST" or abs(time.time() - proof["iat"]) > PROOF_MAX_AGE:
            return refuse(401, "proof of possession is not valid")
        if not store.set(f"prov:proof:{proof['jti']}", "1", nx=True, ex=PROOF_MAX_AGE * 2):
            return refuse(401, "proof of possession has already been used")

        tools = role_tools(agent["role"])
        if tools is None or body.tool not in tools:
            audit(body.agent_id, "TOKEN_REFUSED", "DENIED",
                  {"tool": body.tool, "role": agent["role"], "reason": "tool_not_in_role"})
            return refuse(403, "tool is not permitted for this agent's role")

        resp = http.post(f"{TESSERA_URL}/tokens/request", headers=ADMIN, json={
            "agent_id": body.agent_id, "tool": body.tool, "role": agent["role"],
            "duration_minutes": TOKEN_TTL_MINUTES, "session_id": f"{body.agent_id}:{body.tool}",
            "memory_state": "provisioned", "client_public_key": agent["public_key_pem"]})
        issued = resp.json() if resp.status_code == 200 else {}
        if not issued.get("token"):
            return refuse(502, f"identity service refused the token: HTTP {resp.status_code}")
        try:
            audit(body.agent_id, "TOKEN_ISSUED", "SUCCESS",
                  {"tool": body.tool, "role": agent["role"], "token_jti": issued.get("jti"),
                   "expires_at": issued.get("expires_at")})
        except Exception:  # noqa: BLE001  an unrecorded token must not circulate
            http.post(f"{TESSERA_URL}/tokens/revoke", headers=ADMIN,
                      json={"jti": issued.get("jti"), "reason": "issuance could not be audited"})
            raise
        return {"token": issued["token"], "jti": issued.get("jti"), "expires_at": issued.get("expires_at"),
                "tool": body.tool, "role": agent["role"]}
    except Exception:  # noqa: BLE001
        log.exception("token issuance failed agent=%s", body.agent_id)
        return refuse(503, "token could not be issued")


@app.post("/v1/tokens/revoke")
def revoke(body: Revocation, authorization: Optional[str] = Header(None)):
    if not is_operator(authorization):
        return refuse(401, "operator credential required")
    try:
        resp = http.post(f"{TESSERA_URL}/tokens/revoke", headers=ADMIN,
                         json={"jti": body.jti, "reason": body.reason})
        if resp.status_code != 200:
            return refuse(502, f"identity service refused revocation: HTTP {resp.status_code}")
        audit("operator", "TOKEN_REVOKED", "SUCCESS", {"token_jti": body.jti, "reason": body.reason})
        return {"revoked": True, "jti": body.jti}
    except Exception:  # noqa: BLE001
        log.exception("revocation failed jti=%s", body.jti)
        return refuse(503, "revocation could not be completed")


@app.get("/health")
def health():
    return {"status": "ok", "service": "provisioner"}
