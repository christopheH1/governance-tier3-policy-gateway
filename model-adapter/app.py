"""Model adapter: the only component that talks to model providers.

It sits behind the policy gateway and holds the provider keys. The gateway sends
it a request that policy has already allowed, and the adapter translates it for
the provider using the LiteLLM library (MIT licence; the LiteLLM proxy and its
enterprise package are not used).

It makes no decisions of its own. It accepts calls only with the adapter key,
which only the policy gateway holds.
"""
import hmac
import json
import logging
import os
from typing import Any, Optional

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")   # no download of price tables at start

import litellm
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

litellm.telemetry = False
litellm.drop_params = True
litellm.suppress_debug_info = True

log = logging.getLogger("model-adapter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ADAPTER_KEY = os.environ["MODEL_ADAPTER_KEY"]
with open(os.environ.get("MODEL_ROUTES", "routes.json")) as handle:
    ROUTES = json.load(handle)

app = FastAPI(title="Model adapter", docs_url=None, redoc_url=None, openapi_url=None)


class ChatRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    max_tokens: int = Field(gt=0)
    tools: Optional[list[dict[str, Any]]] = None
    temperature: Optional[float] = None


@app.post("/v1/chat/completions")
def chat(body: ChatRequest, authorization: Optional[str] = Header(None)):
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(key, ADAPTER_KEY):
        return JSONResponse(status_code=401, content={"error": "adapter key required"})
    route = ROUTES.get(body.model)
    if route is None:
        return JSONResponse(status_code=404, content={"error": "no route for this model"})
    if route.get("scripted"):
        return scripted_completion(body)
    if route.get("checker"):
        return checker_completion(body)

    arguments: dict[str, Any] = {"model": route.get("model"), "messages": body.messages,
                                 "max_tokens": body.max_tokens, "timeout": 120}
    if body.tools:
        arguments["tools"] = body.tools
    if body.temperature is not None:
        arguments["temperature"] = body.temperature
    if "mock_response" in route:
        arguments.update(mock_response=route["mock_response"], api_key="not-used")
    elif route.get("local"):      # a model inside the organisation, served by Ollama: no key, no route out
        arguments.update(model=f"ollama_chat/{os.environ.get('OLLAMA_MODEL', 'llama3.2:3b')}",
                         api_base=os.environ.get("OLLAMA_URL", "http://ollama:11434"), timeout=120)
    else:
        api_key = os.environ.get(route["api_key_env"], "")
        if not api_key:
            return JSONResponse(status_code=503, content={"error": f"{route['api_key_env']} is not set"})
        arguments["api_key"] = api_key
    try:
        completion = litellm.completion(**arguments)
    except Exception as exc:  # noqa: BLE001  never pass provider error text on: it can echo request details
        log.warning("provider call failed model=%s error=%s", body.model, type(exc).__name__)
        return JSONResponse(status_code=502, content={"error": type(exc).__name__})
    return completion.model_dump()


SCRIPTED_ARGUMENTS = {
    "database_read": {"table": "sales", "limit": 5},
    "file_read": {"path": "q4-notes.txt"},
    "report_generation": {"title": "Q4 market research report", "summary": "Sales by region for the fourth quarter."},
    "data_export": {"dataset": "q4_market_research_report", "format": "pdf"},
}


def checker_completion(body: ChatRequest) -> dict:
    """A stand-in for the model that checks whether a request fits its purpose, so the
    check can be tested without a real model. It knows one rule: requests about food do not fit."""
    asked = str(body.messages[-1].get("content", ""))
    latest = asked.split("Latest request:", 1)[-1].lower()
    off_topic = any(word in latest for word in ("pizza", "recipe", "olives", "pasta"))
    text = "MISMATCH: the request is about food, not the declared purpose." if off_topic \
        else "FITS: nothing in the request is outside the declared purpose."
    return {"id": "checker", "object": "chat.completion", "model": body.model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": len(asked) // 4, "completion_tokens": 12, "total_tokens": len(asked) // 4 + 12}}


def scripted_completion(body: ChatRequest) -> dict:
    """A stand-in model that follows a fixed script, so an agent can be shown working
    without a provider key or any cost. It calls each tool it has been offered once,
    in order, then reports what came back. It does not understand the task."""
    offered = [(tool.get("function") or {}).get("name") for tool in body.tools or []]
    called = [call["function"]["name"] for message in body.messages
              for call in (message.get("tool_calls") or [])]
    remaining = [name for name in offered if name and name not in called]
    if remaining:
        name = remaining[0]
        message = {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{len(called) + 1}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(SCRIPTED_ARGUMENTS.get(name, {}))}}]}
        finish = "tool_calls"
    else:
        results = [str(message.get("content"))[:160] for message in body.messages if message.get("role") == "tool"]
        text = ("Done. I used " + ", ".join(called) + ". Results: " + " | ".join(results)) if called \
            else "I was not offered any tool I could use for this task, so there is nothing to report."
        message = {"role": "assistant", "content": text}
        finish = "stop"
    prompt_tokens = len(json.dumps(body.messages)) // 4
    return {"id": "scripted", "object": "chat.completion", "model": body.model,
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20, "total_tokens": prompt_tokens + 20}}


@app.get("/health")
def health():
    return {"status": "ok", "service": "model-adapter", "models": sorted(ROUTES)}
