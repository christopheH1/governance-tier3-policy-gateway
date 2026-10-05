"""Agent runtime: hosts the agents of the Research Operations scenario.

An agent here is a loop: ask the model what to do, carry out the tool calls it
proposes, give the results back, and repeat until the model has an answer. Every
model call and every tool call goes through the policy gateway. The runtime has
no other route: it sits on the agents network only.

Each agent holds its own private key, which never leaves this service. An
operator enrols the matching public key with the provisioner. The runtime holds
no operator or reviewer credential.

It is also the bridge for a workflow application such as LangGraph, which speaks
two standard protocols and cannot speak the gateway's own:
  POST /v1/chat/completions   the OpenAI chat format, for models
  POST /mcp                   the Model Context Protocol over HTTP, for tools
The application presents one API key per agent. The bridge maps the key to the
agent, attaches the agent's token, proof of key, and declared purpose, and sends
the call to the policy gateway. The bridge decides nothing.
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

log = logging.getLogger("agent-runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

GATEWAY = os.environ.get("GATEWAY_URL", "http://policy-gateway:9090").rstrip("/")
PROVISIONER = os.environ.get("PROVISIONER_URL", "http://provisioner:9080").rstrip("/")
RUNTIME_KEY = os.environ["AGENT_RUNTIME_KEY"]
KEY_DIR = Path(os.environ.get("AGENT_KEY_DIR", "/keys"))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))
HOLD_POLL_SECONDS = float(os.environ.get("HOLD_POLL_SECONDS", "3"))
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
BRIDGE_HOLD_WAIT_SECONDS = float(os.environ.get("BRIDGE_HOLD_WAIT_SECONDS", "40"))   # under the usual 60 s tool timeout
BRIDGE_TASK_IDLE_SECONDS = float(os.environ.get("BRIDGE_TASK_IDLE_SECONDS", "300"))
CHAT_URL, INVOKE_URL, TOKEN_URL = f"{GATEWAY}/v1/models/chat", f"{GATEWAY}/v1/tools/invoke", f"{PROVISIONER}/v1/tokens"

with open(os.environ.get("AGENTS_FILE", "agents.json")) as handle:
    CONFIG = json.load(handle)
TOOL_DEFINITIONS = CONFIG["tool_definitions"]
http = httpx.Client(timeout=httpx.Timeout(150.0, connect=3.0))
app = FastAPI(title="Agent runtime", docs_url=None, redoc_url=None, openapi_url=None)


class Agent:
    def __init__(self, spec: dict):
        self.spec = spec
        self.agent_id = spec["id"]
        self.tokens: dict[str, tuple[str, float]] = {}
        path = KEY_DIR / f"{self.agent_id}.pem"
        if path.exists():
            self.key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            KEY_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
            path.chmod(0o600)
        public = self.key.public_key()
        self.public_pem = public.public_bytes(serialization.Encoding.PEM,
                                              serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        numbers = public.public_numbers()
        b64 = lambda value: jwt.utils.base64url_encode(value.to_bytes(32, "big")).decode()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64(numbers.x), "y": b64(numbers.y)}

    def proof(self, url: str) -> str:
        claims = {"htu": url, "htm": "POST", "iat": int(time.time()), "jti": uuid.uuid4().hex}
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": self.jwk})

    def token(self, capability: str) -> tuple[Optional[str], str]:
        """A token for one tool or for the model path. Returns (token, why not)."""
        held = self.tokens.get(capability)
        if held and time.time() - held[1] < 600:
            return held[0], ""
        resp = http.post(TOKEN_URL, headers={"DPoP": self.proof(TOKEN_URL)},
                         json={"agent_id": self.agent_id, "tool": capability})
        if resp.status_code == 200:
            self.tokens[capability] = (resp.json()["token"], time.time())
            return self.tokens[capability][0], ""
        try:
            detail = resp.json().get("detail", "")
        except ValueError:
            detail = ""
        return None, detail or f"HTTP {resp.status_code}"

    def headers(self, token: str, url: str) -> dict:
        return {"Authorization": f"DPoP {token}", "DPoP": self.proof(url)}

    def public(self) -> dict:
        return {key: self.spec[key] for key in ("id", "name", "role", "purposes", "models", "tools")} | {
            "public_key_pem": self.public_pem}


AGENTS = {spec["id"]: Agent(spec) for spec in CONFIG["agents"]}
RUNS: dict[str, dict] = {}
RUN_ORDER: list[str] = []
CONVERSATIONS: dict[str, list] = {}   # run id -> the messages so far, so a person can reply and the agent carries on
LOCK = threading.Lock()


def add_step(run: dict, kind: str, title: str, outcome: str = "", **detail) -> dict:
    step = {"n": len(run["steps"]) + 1, "kind": kind, "title": title, "outcome": outcome,
            "at": time.time()} | {k: v for k, v in detail.items() if v not in (None, "", [])}
    with LOCK:
        run["steps"].append(step)
    return step


def tool_schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, **TOOL_DEFINITIONS[name]}}


def call_tool(agent: Agent, run: dict, intent: dict, name: str, params: dict) -> str:
    """Carry out one tool call through the gateway. Returns the text handed back to the model."""
    token, why = agent.token(name)
    if token is None:
        add_step(run, "tool", name, "deny", params=params, reasons=["no_token_for_this_tool"],
                 note=f"The provisioner refused a token: {why}")
        return json.dumps({"error": "not permitted", "detail": "this agent may not use this tool"})
    payload = {"intent": intent, "tool": {"name": name, "params": params}}
    resp = http.post(INVOKE_URL, headers=agent.headers(token, INVOKE_URL), json=payload)
    body = resp.json()
    if body.get("outcome") == "refer" and body.get("status") == "pending":
        step = add_step(run, "tool", name, "hold", params=params, reasons=body.get("reasons"),
                        hold_reference=body["hold_reference"], expires_at=body.get("expires_at"),
                        trace_id=body.get("trace_id"), note="Waiting for a reviewer")
        run["status"] = "waiting for review"
        payload["hold_reference"] = body["hold_reference"]
        while body.get("status") == "pending":
            time.sleep(HOLD_POLL_SECONDS)
            resp = http.post(INVOKE_URL, headers=agent.headers(token, INVOKE_URL), json=payload)
            body = resp.json()
        run["status"] = "running"
        released = body.get("outcome") == "allow"
        step["note"] = "Approved by a reviewer, then carried out" if released else "Not approved"
        step["outcome"] = "allow" if released else "deny"
        step["reasons"] = body.get("reasons", step.get("reasons"))
        if released:
            step["result"] = body.get("result")
            return json.dumps(body.get("result"))
        return json.dumps({"error": "not approved", "reasons": body.get("reasons")})
    outcome = "flag" if body.get("mode") == "flag" else body.get("outcome", "deny")
    add_step(run, "tool", name, outcome, params=params, reasons=body.get("reasons"),
             result=body.get("result"), trace_id=body.get("trace_id"))
    if "result" in body:
        return json.dumps(body["result"])
    return json.dumps({"error": "refused by policy", "reasons": body.get("reasons")})


def execute(run: dict) -> None:
    agent = AGENTS[run["agent_id"]]
    intent = {"purpose": run["purpose"], "task_id": run["id"], "statement": run["task"][:200]}
    if run.get("acting_for"):      # the person whose instruction the agent is carrying out, as the console verified them
        intent["on_behalf_of"] = run["acting_for"]
    messages = CONVERSATIONS.setdefault(run["id"], [{"role": "system", "content": agent.spec["brief"]},
                                                    {"role": "user", "content": run["task"]}])
    tools = [tool_schema(name) for name in agent.spec["tools"]]
    try:
        for _ in range(MAX_STEPS):
            token, why = agent.token("model_access")
            if token is None:
                add_step(run, "model", run["model"], "deny", reasons=["no_token_for_the_model_path"], note=why)
                run["status"] = "stopped"
                return
            resp = http.post(CHAT_URL, headers=agent.headers(token, CHAT_URL),
                             json={"intent": intent, "model": run["model"], "messages": messages,
                                   "tools": tools, "max_tokens": 400})
            body = resp.json()
            if body.get("outcome") != "allow":
                add_step(run, "model", run["model"], "deny", reasons=body.get("reasons"), trace_id=body.get("trace_id"))
                run["status"] = "stopped"
                return
            message = body["completion"]["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            governance = body.get("governance", {})
            add_step(run, "model", run["model"], "allow", trace_id=body.get("trace_id"),
                     tokens=(body["completion"].get("usage") or {}).get("total_tokens"),
                     tools_offered=governance.get("tools_offered"), tools_removed=governance.get("tools_removed"),
                     proposed=[call["function"]["name"] for call in calls], text=message.get("content"))
            if not calls:
                run["answer"] = message.get("content") or ""
                run["answered_at"] = time.time()
                if run["answer"]:
                    messages.append({"role": "assistant", "content": run["answer"]})
                run["status"] = "finished"
                return
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                try:
                    params = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    params = {}
                result = call_tool(agent, run, intent, call["function"]["name"], params)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        add_step(run, "note", "Stopped", "deny", note=f"The agent reached its limit of {MAX_STEPS} model calls")
        run["status"] = "stopped"
    except Exception as exc:  # noqa: BLE001
        log.exception("run failed run=%s", run["id"])
        add_step(run, "note", "The run failed", "deny", note=type(exc).__name__)
        run["status"] = "failed"
    finally:
        run["finished_at"] = time.time()


class RunRequest(BaseModel):
    agent_id: str
    purpose: str
    model: str
    task: str = Field(min_length=3, max_length=2000)
    requested_by: Optional[str] = Field(default=None, max_length=128)


def authorised(authorization: Optional[str]) -> bool:
    scheme, _, key = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(key, RUNTIME_KEY)


@app.get("/v1/agents")
def list_agents(authorization: Optional[str] = Header(None)):
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    return {"agents": [agent.public() for agent in AGENTS.values()]}


@app.post("/v1/runs")
def start_run(body: RunRequest, authorization: Optional[str] = Header(None)):
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    agent = AGENTS.get(body.agent_id)
    if agent is None:
        return JSONResponse(status_code=404, content={"detail": "no such agent"})
    if body.model not in agent.spec["models"]:
        return JSONResponse(status_code=422, content={"detail": "this agent is not set up for that model"})
    run = {"id": f"run-{uuid.uuid4().hex[:10]}", "agent_id": body.agent_id, "agent_name": agent.spec["name"],
           "purpose": body.purpose, "model": body.model, "task": body.task, "status": "running",
           "requested_by": body.requested_by, "acting_for": body.requested_by,
           "started_at": time.time(), "steps": [], "answer": None}
    with LOCK:
        RUNS[run["id"]] = run
        RUN_ORDER.append(run["id"])
        for old in RUN_ORDER[:-50]:
            RUNS.pop(old, None)
            CONVERSATIONS.pop(old, None)
        del RUN_ORDER[:-50]
    threading.Thread(target=execute, args=(run,), daemon=True).start()
    return {"id": run["id"]}


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    person: str = Field(default="someone", max_length=128)


@app.post("/v1/runs/{run_id}/reply")
def reply_to_run(run_id: str, body: Reply, authorization: Optional[str] = Header(None)):
    """A person answers the agent, and the agent carries on with the same task. The reply is
    one more message to the model, so it passes through the policy gateway like every other."""
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    run = RUNS.get(run_id)
    if run is None:
        return JSONResponse(status_code=404, content={"detail": "no such run"})
    with LOCK:
        if run_id not in CONVERSATIONS:
            return JSONResponse(status_code=409, content={"detail": "this task was not started here, so it cannot be replied to"})
        if run["status"] != "finished":
            return JSONResponse(status_code=409, content={"detail": "the agent is not waiting for a reply"})
        run["status"] = "running"
    if run.get("answer"):
        add_step(run, "agent", "The agent answered", text=run["answer"], at=run.get("answered_at"))
    add_step(run, "person", f"{body.person} replied", text=body.text)
    run["answer"] = None
    run["acting_for"] = body.person      # from here on the agent acts on this person's instruction
    CONVERSATIONS[run_id].append({"role": "user", "content": body.text})
    threading.Thread(target=execute, args=(run,), daemon=True).start()
    return {"id": run_id}


@app.get("/v1/runs")
def list_runs(authorization: Optional[str] = Header(None)):
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    with LOCK:
        return {"runs": [{k: RUNS[i].get(k) for k in ("id", "agent_name", "purpose", "model", "task", "status",
                                                      "started_at", "requested_by")}
                         | {"can_reply": i in CONVERSATIONS}
                         for i in reversed(RUN_ORDER) if i in RUNS]}


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, authorization: Optional[str] = Header(None)):
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    run = RUNS.get(run_id)
    return run | {"can_reply": run_id in CONVERSATIONS} if run else JSONResponse(status_code=404, content={"detail": "no such run"})


# --- bridge for a workflow application ------------------------------------------
def agent_api_key(agent_id: str) -> str:
    """Each agent's API key is derived from one secret, so there is nothing else to store."""
    return "agk_" + hmac.new(BRIDGE_SECRET.encode(), agent_id.encode(), hashlib.sha256).hexdigest()[:40]


def agent_for(authorization: Optional[str]) -> Optional[Agent]:
    scheme, _, key = (authorization or "").partition(" ")
    if not BRIDGE_SECRET or scheme.lower() != "bearer":
        return None
    for agent in AGENTS.values():
        if hmac.compare_digest(key.strip(), agent_api_key(agent.agent_id)):
            return agent
    return None


TASKS: dict[str, dict] = {}          # agent id -> the task its calls currently belong to
PENDING_HOLDS: dict[str, dict] = {}  # agent, tool and parameters -> a hold still waiting for a reviewer


def context_of(headers) -> dict:
    """What the workflow application may say about a call: its purpose and, if it wants, which task it belongs to."""
    named = headers.get("x-task-id") or ""
    person = (headers.get("x-on-behalf-of") or "").strip()[:128]
    return {"purpose": headers.get("x-purpose"), "person": person,
            "task": named if named and len(named) <= 64 and all(c.isalnum() or c in "-_" for c in named) else ""}


def bridge_task(agent: Agent, context: dict, model: str = "", statement: str = "") -> tuple[dict, dict]:
    """Calls from one agent belong to one task: the one the application names, or else
    whatever it does until it has been idle for a while. Several agents may share a named task.
    Returns the run shown in the console and the intent sent to the gateway."""
    purpose = context.get("purpose") or agent.spec["purposes"][0]
    named = context.get("task") or ""
    slot = f"{agent.agent_id}/{named}"
    with LOCK:
        task = TASKS.get(slot)
        if task is None or task["purpose"] != purpose or (not named and time.time() - task["seen"] > BRIDGE_TASK_IDLE_SECONDS):
            task_id = named or f"flow-{uuid.uuid4().hex[:10]}"
            run = {"id": f"{task_id}.{agent.agent_id}" if named else task_id, "task_id": task_id, "agent_id": agent.agent_id,
                   "agent_name": agent.spec["name"] + " (workflow)", "purpose": purpose, "model": model or "-",
                   "task": statement or "Started from the workflow application", "status": "driven by the workflow application",
                   "started_at": time.time(), "steps": [], "answer": None}
            RUNS[run["id"]] = run
            RUN_ORDER.append(run["id"])
            for old in RUN_ORDER[:-50]:
                RUNS.pop(old, None)
            del RUN_ORDER[:-50]
            task = TASKS[slot] = {"run": run, "purpose": purpose, "statement": statement}
            for old in [k for k, v in TASKS.items() if time.time() - v.get("seen", time.time()) > 86400]:
                TASKS.pop(old, None)
        task["seen"] = time.time()
        if statement and not task["statement"]:
            task["statement"] = task["run"]["task"] = statement
        if model:
            task["run"]["model"] = model
    if context.get("person"):
        task["run"]["requested_by"] = task["run"].get("requested_by") or context["person"]
    intent = {"purpose": purpose, "task_id": task["run"]["task_id"], "on_behalf_of": context.get("person"), "statement": (task["statement"] or "")[:200]}
    return task["run"], {key: value for key, value in intent.items() if value}


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return ""


def openai_error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": "policy_gateway", "code": code}})


@app.get("/v1/models")
def bridge_models(authorization: Optional[str] = Header(None)):
    agent = agent_for(authorization)
    if agent is None:
        return openai_error(401, "An agent API key is required.", "invalid_api_key")
    return {"object": "list", "data": [{"id": name, "object": "model", "owned_by": "policy-gateway"}
                                       for name in agent.spec["models"]]}


@app.post("/v1/chat/completions")
async def bridge_chat(request: Request):
    from starlette.concurrency import run_in_threadpool
    agent = agent_for(request.headers.get("authorization"))
    if agent is None:
        return openai_error(401, "An agent API key is required.", "invalid_api_key")
    try:
        body = await request.json()
        model, messages = body["model"], body["messages"]
        assert isinstance(model, str) and isinstance(messages, list) and messages
    except Exception:  # noqa: BLE001
        return openai_error(400, "The request needs a model and messages.", "invalid_request")
    return await run_in_threadpool(bridge_chat_call, agent, body, context_of(request.headers))


def bridge_chat_call(agent: Agent, body: dict, context: dict):
    model, messages = body["model"], body["messages"]
    # Some clients send text as a list of parts. Models inside the organisation expect plain text.
    messages = [{**m, "content": "\n".join(str(part.get("text", "")) for part in m["content"])}
                if isinstance(m, dict) and isinstance(m.get("content"), list)
                and all(isinstance(part, dict) and part.get("type") == "text" for part in m["content"]) else m
                for m in messages]
    asked = next((text_of(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    run, intent = bridge_task(agent, context, model, asked.strip()[:200])
    token, why = agent.token("model_access")
    if token is None:
        add_step(run, "model", model, "deny", reasons=["no_token_for_the_model_path"], note=why)
        return openai_error(403, f"The provisioner refused a token for the model path: {why}", "no_token")
    payload = {"intent": intent, "model": model, "messages": messages}
    limit = body.get("max_completion_tokens") or body.get("max_tokens")
    if limit:
        payload["max_tokens"] = int(limit)
    if body.get("tools"):
        payload["tools"] = body["tools"]
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    try:
        answer = http.post(CHAT_URL, headers=agent.headers(token, CHAT_URL), json=payload).json()
    except Exception as exc:  # noqa: BLE001
        log.warning("gateway call failed: %s", type(exc).__name__)
        return openai_error(502, "The policy gateway could not be reached.", "gateway_unavailable")
    if answer.get("outcome") != "allow":
        reasons = answer.get("reasons") or ["refused"]
        add_step(run, "model", model, "deny", reasons=reasons, trace_id=answer.get("trace_id"))
        return openai_error(403, "Refused by the policy gateway: " + ", ".join(map(str, reasons))
                            + f" (trace {answer.get('trace_id')})", str(reasons[0]))
    completion = answer["completion"]
    message = completion["choices"][0]["message"]
    calls = message.get("tool_calls") or []
    governance = answer.get("governance", {})
    add_step(run, "model", model, "allow", trace_id=answer.get("trace_id"),
             tokens=(completion.get("usage") or {}).get("total_tokens"),
             tools_offered=governance.get("tools_offered"), tools_removed=governance.get("tools_removed"),
             proposed=[call["function"]["name"] for call in calls], text=message.get("content"))
    if not calls and message.get("content"):
        run["answer"] = message["content"]
    completion.setdefault("id", f"chatcmpl-{uuid.uuid4().hex}")
    completion.setdefault("object", "chat.completion")
    completion.setdefault("created", int(time.time()))
    completion["model"] = model
    if not body.get("stream"):
        return JSONResponse(content=completion)

    # The gateway does not stream. The whole answer is sent as one streamed piece.
    base = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"], "model": model}
    delta: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if calls:
        delta["tool_calls"] = [{"index": index, "id": call.get("id"), "type": "function",
                                "function": {"name": call["function"]["name"],
                                             "arguments": call["function"].get("arguments") or "{}"}}
                               for index, call in enumerate(calls)]
    finish = completion["choices"][0].get("finish_reason") or ("tool_calls" if calls else "stop")
    chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
    if (body.get("stream_options") or {}).get("include_usage") and completion.get("usage"):
        chunks.append({**base, "choices": [], "usage": completion["usage"]})

    def stream():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")


def bridge_tool_call(agent: Agent, context: dict, name: str, params: dict,
                     wait: float = BRIDGE_HOLD_WAIT_SECONDS) -> tuple[str, bool]:
    """One tool call from the workflow application. Returns (text for the model, is it an error)."""
    if name not in TOOL_DEFINITIONS:
        return json.dumps({"error": "no such tool"}), True
    run, intent = bridge_task(agent, context)
    pending_key = hashlib.sha256(json.dumps([agent.agent_id, context.get("task") or "", name, params],
                                            sort_keys=True).encode()).hexdigest()
    token, why = agent.token(name)
    if token is None:
        add_step(run, "tool", name, "deny", params=params, reasons=["no_token_for_this_tool"],
                 note=f"The provisioner refused a token: {why}")
        return json.dumps({"error": "not permitted", "detail": "this agent may not use this tool"}), True
    waiting = PENDING_HOLDS.get(pending_key)
    payload: dict[str, Any] = {"intent": intent, "tool": {"name": name, "params": params}}
    step = None
    if waiting:     # the same call was held earlier: ask about that hold, with the intent it was made under
        payload = {"intent": waiting["intent"], "tool": {"name": name, "params": params},
                   "hold_reference": waiting["hold_reference"]}
        step = waiting["step"]
    body = http.post(INVOKE_URL, headers=agent.headers(token, INVOKE_URL), json=payload).json()
    if body.get("outcome") == "refer" and body.get("status") == "pending":
        if step is None:
            step = add_step(run, "tool", name, "hold", params=params, reasons=body.get("reasons"),
                            hold_reference=body["hold_reference"], expires_at=body.get("expires_at"),
                            trace_id=body.get("trace_id"), note="Waiting for a reviewer")
            PENDING_HOLDS[pending_key] = {"hold_reference": body["hold_reference"], "intent": payload["intent"], "step": step}
            payload["hold_reference"] = body["hold_reference"]
        deadline = time.time() + wait
        while body.get("status") == "pending" and time.time() < deadline:
            time.sleep(HOLD_POLL_SECONDS)
            body = http.post(INVOKE_URL, headers=agent.headers(token, INVOKE_URL), json=payload).json()
        if body.get("status") == "pending":
            return json.dumps({"status": "held for review", "hold_reference": payload["hold_reference"],
                               "detail": "This call has NOT run. A reviewer must approve it first. "
                                         "Tell the user it is waiting for review. The same call can be repeated "
                                         "once it has been approved."}), True
    if step is not None:
        PENDING_HOLDS.pop(pending_key, None)
        released = body.get("outcome") == "allow"
        step["note"] = "Approved by a reviewer, then carried out" if released else "Not approved"
        step["outcome"] = "allow" if released else "deny"
        step["reasons"] = body.get("reasons", step.get("reasons"))
        if released:
            step["result"] = body.get("result")
            return json.dumps(body.get("result")), False
        return json.dumps({"error": "not approved", "reasons": body.get("reasons")}), True
    outcome = "flag" if body.get("mode") == "flag" else body.get("outcome", "deny")
    add_step(run, "tool", name, outcome, params=params, reasons=body.get("reasons"),
             result=body.get("result"), trace_id=body.get("trace_id"))
    if "result" in body:
        return json.dumps(body["result"]), False
    return json.dumps({"error": "refused by policy", "reasons": body.get("reasons")}), True


MCP_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def mcp_answer(agent: Agent, context: dict, message: dict, wait: float = BRIDGE_HOLD_WAIT_SECONDS) -> Optional[dict]:
    """Answers one JSON-RPC message. Notifications get no answer."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    def ok(result):
        return {"jsonrpc": "2.0", "id": ident, "result": result}
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "policy-gateway-bridge", "version": "1.0"},
                   "instructions": "Every tool call is decided by the policy gateway. A call may be refused or held for a reviewer."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": name, "description": TOOL_DEFINITIONS[name]["description"],
                              "inputSchema": TOOL_DEFINITIONS[name]["parameters"]} for name in agent.spec["tools"]]})
    if method == "tools/call":
        params = message.get("params") or {}
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            text, failed = bridge_tool_call(agent, context, str(params.get("name")), arguments, wait)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool call through the gateway failed: %s", type(exc).__name__)
            text, failed = json.dumps({"error": "the policy gateway could not be reached"}), True
        return ok({"content": [{"type": "text", "text": text}], "isError": failed})
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}}


@app.post("/mcp")
async def mcp(request: Request):
    from starlette.concurrency import run_in_threadpool
    agent = agent_for(request.headers.get("authorization"))
    if agent is None:
        return JSONResponse(status_code=401, content={"detail": "an agent API key is required"})
    try:
        message = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None,
                                                      "error": {"code": -32700, "message": "parse error"}})
    context = context_of(request.headers)
    try:     # a caller may choose to wait less for a reviewer, never more
        wait = min(BRIDGE_HOLD_WAIT_SECONDS, max(0.0, float(request.headers.get("x-hold-wait", BRIDGE_HOLD_WAIT_SECONDS))))
    except ValueError:
        wait = BRIDGE_HOLD_WAIT_SECONDS
    batch = message if isinstance(message, list) else [message]
    answers = [a for a in [await run_in_threadpool(mcp_answer, agent, context, m, wait) for m in batch
                           if isinstance(m, dict)] if a is not None]
    if not answers:
        return Response(status_code=202)
    return JSONResponse(content=answers if isinstance(message, list) else answers[0])


@app.get("/mcp")
@app.delete("/mcp")
def mcp_no_stream():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/v1/bridge/keys")
def bridge_keys(authorization: Optional[str] = Header(None)):
    """For the operator: the API key to paste into the workflow application for each agent."""
    if not authorised(authorization):
        return JSONResponse(status_code=401, content={"detail": "runtime key required"})
    return {"agents": [{"id": a.agent_id, "name": a.spec["name"], "purposes": a.spec["purposes"],
                        "models": a.spec["models"], "tools": a.spec["tools"],
                        "api_key": agent_api_key(a.agent_id)} for a in AGENTS.values()] if BRIDGE_SECRET else []}


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-runtime", "agents": sorted(AGENTS)}
