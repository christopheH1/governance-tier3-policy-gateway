"""Console: the human side of the Research Operations scenario.

Three human roles use it:
  requester  gives the agents tasks and replies to them
  reviewer   approves or rejects calls the policy gateway has held
  operator   enrols the agents

The console holds the operator and reviewer credentials, so it is kept apart from
the agent runtime, which holds neither. It makes no decisions of its own: tasks go
to the agent runtime, and review decisions go to the policy gateway.

Each person signs in with their own account (see users.py). The name is passed on
with everything they do, so the audit ledger names who asked for a task, who replied,
and who reviewed. The console is where that name is verified.
"""
import base64
import hashlib
import hmac
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

GATEWAY = os.environ.get("GATEWAY_URL", "http://policy-gateway:9090").rstrip("/")
PROVISIONER = os.environ.get("PROVISIONER_URL", "http://provisioner:9080").rstrip("/")
RUNTIME = os.environ.get("AGENT_RUNTIME_URL", "http://agent-runtime:9070").rstrip("/")
import users as accounts

# Accounts for the automated tests only. They exist while CONSOLE_TEST_ACCOUNTS is "true"
# and share CONSOLE_PASSWORD. No other name can sign in with that password.
TEST_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "")
TEST_ACCOUNTS = {"e2e.reviewer": list(accounts.ROLES), "workflow.reviewer": list(accounts.ROLES),
                 "e2e.requester": ["requester"]} \
    if os.environ.get("CONSOLE_TEST_ACCOUNTS", "false").lower() == "true" and TEST_PASSWORD else {}
_verified: dict[str, tuple] = {}     # remembers a good sign-in, so the slow password check runs once per session
OPERATOR = {"Authorization": f"Bearer {os.environ['PROVISIONER_OPERATOR_KEY']}"}
REVIEWER = {"Authorization": f"Bearer {os.environ['GATEWAY_REVIEWER_KEY']}"}
RUNTIME_AUTH = {"Authorization": f"Bearer {os.environ['AGENT_RUNTIME_KEY']}"}
PAGE = (Path(__file__).parent / "index.html").read_text()

http = httpx.Client(timeout=httpx.Timeout(20.0, connect=3.0))
app = FastAPI(title="Console", docs_url=None, redoc_url=None, openapi_url=None)
enrolled: dict[str, list] = {}


def signed_in(authorization: Optional[str]) -> Optional[tuple]:
    """Returns (name, roles) for a person whose name and password match an account, or None."""
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        name, _, password = base64.b64decode(encoded).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    name = name.strip()[:80]
    if not name or not password:
        return None
    if name in TEST_ACCOUNTS:
        return (name, TEST_ACCOUNTS[name]) if hmac.compare_digest(password, TEST_PASSWORD) else None
    try:
        stamp = accounts.USERS_FILE.stat().st_mtime_ns
    except OSError:
        stamp = 0
    proof = hashlib.sha256(f"{name}\0{password}".encode()).hexdigest()
    known = _verified.get(name)
    if known and known[0] == stamp and hmac.compare_digest(known[1], proof):
        return name, known[2]
    roles = accounts.check(accounts.load(), name, password)
    if roles is None:
        _verified.pop(name, None)
        return None
    _verified[name] = (stamp, proof, roles)
    return name, roles


def needs(request: Request, role: str) -> Optional[JSONResponse]:
    if role in request.state.roles:
        return None
    return JSONResponse(status_code=403, content={
        "detail": f"Your account does not have the {role} role, which this needs."})


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    person = signed_in(request.headers.get("authorization"))
    if person is None:
        return Response(status_code=401, content="Sign in with your own console account.",
                        headers={"WWW-Authenticate": 'Basic realm="Agent console", charset="UTF-8"'})
    # A custom header on every change stops another web page from acting through this browser.
    if request.method != "GET" and request.headers.get("x-console") != "1":
        return JSONResponse(status_code=403, content={"detail": "request did not come from the console page"})
    request.state.person, request.state.roles = person
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def relay(resp: httpx.Response):
    try:
        content = resp.json()
    except ValueError:
        content = {"detail": resp.text[:300]}
    return JSONResponse(status_code=resp.status_code, content=content)


def unreachable(what: str):
    return JSONResponse(status_code=502, content={"detail": f"The {what} could not be reached."})


@app.get("/", response_class=HTMLResponse)
def page():
    return PAGE


@app.get("/api/overview")
def overview(request: Request):
    """Everything the page shows, in one call."""
    result = {"person": request.state.person, "roles": request.state.roles, "agents": [], "runs": [], "holds": [], "flags": [], "problems": []}
    try:
        agents = http.get(f"{RUNTIME}/v1/agents", headers=RUNTIME_AUTH).json().get("agents", [])
        result["agents"] = [{**{k: a[k] for k in ("id", "name", "role", "purposes", "models", "tools")},
                             "enrolled": a["id"] in enrolled, "allowed_tools": enrolled.get(a["id"])} for a in agents]
        result["runs"] = http.get(f"{RUNTIME}/v1/runs", headers=RUNTIME_AUTH).json().get("runs", [])
    except (httpx.HTTPError, ValueError):
        result["problems"].append("The agent runtime could not be reached.")
    try:
        result["holds"] = http.get(f"{GATEWAY}/v1/review/holds", headers=REVIEWER).json().get("pending", [])
        result["flags"] = http.get(f"{GATEWAY}/v1/review/flags", headers=REVIEWER).json().get("flags", [])[:8]
    except (httpx.HTTPError, ValueError):
        result["problems"].append("The policy gateway could not be reached.")
    return result


@app.post("/api/enrol")
def enrol(request: Request):
    """Operator action: register each agent's public key and role with the provisioner."""
    if (refused := needs(request, "operator")) is not None:
        return refused
    try:
        agents = http.get(f"{RUNTIME}/v1/agents", headers=RUNTIME_AUTH).json()["agents"]
    except (httpx.HTTPError, ValueError, KeyError):
        return unreachable("agent runtime")
    outcome = []
    for agent in agents:
        try:
            resp = http.post(f"{PROVISIONER}/v1/agents", headers=OPERATOR, json={
                "agent_id": agent["id"], "role": agent["role"], "owner": request.state.person,
                "public_key_pem": agent["public_key_pem"]})
        except httpx.HTTPError:
            return unreachable("provisioner")
        if resp.status_code == 200:
            enrolled[agent["id"]] = resp.json().get("allowed_tools", [])
        outcome.append({"id": agent["id"], "enrolled": resp.status_code == 200,
                        "detail": "" if resp.status_code == 200 else resp.json().get("detail", "")})
    return {"agents": outcome}


class Task(BaseModel):
    agent_id: str
    purpose: str
    model: str
    task: str = Field(min_length=3, max_length=2000)


@app.post("/api/runs")
def start_run(body: Task, request: Request):
    if (refused := needs(request, "requester")) is not None:
        return refused
    try:
        return relay(http.post(f"{RUNTIME}/v1/runs", headers=RUNTIME_AUTH,
                               json=body.model_dump() | {"requested_by": request.state.person}))
    except httpx.HTTPError:
        return unreachable("agent runtime")


class ReplyText(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@app.post("/api/runs/{run_id}/reply")
def reply(run_id: str, body: ReplyText, request: Request):
    if (refused := needs(request, "requester")) is not None:
        return refused
    try:
        return relay(http.post(f"{RUNTIME}/v1/runs/{run_id}/reply", headers=RUNTIME_AUTH,
                               json={"text": body.text, "person": request.state.person}))
    except httpx.HTTPError:
        return unreachable("agent runtime")


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        return relay(http.get(f"{RUNTIME}/v1/runs/{run_id}", headers=RUNTIME_AUTH))
    except httpx.HTTPError:
        return unreachable("agent runtime")


class Decision(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=300)


@app.post("/api/holds/{hold_id}/decision")
def decide(hold_id: str, body: Decision, request: Request):
    """Reviewer action. The signed-in name is recorded in the audit ledger with the decision."""
    if (refused := needs(request, "reviewer")) is not None:
        return refused
    try:
        return relay(http.post(f"{GATEWAY}/v1/review/holds/{hold_id}/decision", headers=REVIEWER,
                               json={"reviewer": request.state.person, "approve": body.approve, "note": body.note}))
    except httpx.HTTPError:
        return unreachable("policy gateway")


@app.get("/health")
def health():
    return {"status": "ok", "service": "console"}
