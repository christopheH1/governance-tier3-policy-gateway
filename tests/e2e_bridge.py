"""Stage 6 acceptance test: the bridge a workflow application uses.

It plays the workflow application: it speaks the OpenAI chat format and the Model
Context Protocol to the bridge, with one API key per agent, and checks that the
policy gateway still decides everything.

Uses the scripted model, so it needs no provider key and no local model.
Set LOCAL_MODEL_TEST=1 to add one call to the local model (needs Ollama with its model pulled).
The exit code is non-zero if any check fails.
"""
import json
import os
import sys
import threading
import time

import requests

BRIDGE = os.environ.get("BRIDGE_URL", "http://agent-runtime:9070")
CONSOLE = os.environ.get("CONSOLE_URL", "http://console:8088")
AUTH = ("e2e.reviewer", os.environ["CONSOLE_PASSWORD"])
RUNTIME = {"Authorization": f"Bearer {os.environ['AGENT_RUNTIME_KEY']}"}
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def tool(name):
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": {}}}}


def chat(key, model="scripted-model", tools=(), text="Report the Q4 sales figures by region.", **extra):
    headers = {"Authorization": f"Bearer {key}"} | extra.pop("headers", {})
    return requests.post(f"{BRIDGE}/v1/chat/completions", headers=headers, timeout=180, stream=bool(extra.get("stream")),
                         json={"model": model, "messages": [{"role": "user", "content": text}],
                               "tools": [tool(name) for name in tools], **extra})


def mcp(key, method, params=None, ident=1, **headers):
    message = {"jsonrpc": "2.0", "method": method}
    if ident is not None:
        message["id"] = ident
    if params is not None:
        message["params"] = params
    return requests.post(f"{BRIDGE}/mcp", json=message, timeout=120,
                         headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"} | headers)


def call(key, name, arguments, **headers):
    result = mcp(key, "tools/call", {"name": name, "arguments": arguments}, **headers).json().get("result", {})
    text = (result.get("content") or [{}])[0].get("text", "")
    return result.get("isError"), text


def holds():
    return requests.get(f"{CONSOLE}/api/overview", auth=AUTH, timeout=30).json()["holds"]


def decide(reference, approve):
    return requests.post(f"{CONSOLE}/api/holds/{reference}/decision", json={"approve": approve}, auth=AUTH,
                         headers={"X-Console": "1"}, timeout=30)


def main():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if requests.get(f"{BRIDGE}/health", timeout=3).status_code == 200 \
                    and requests.get(f"{CONSOLE}/health", timeout=3).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(2)

    print("--- Operator: enrol the agents and read their API keys")
    r = requests.post(f"{CONSOLE}/api/enrol", json={}, auth=AUTH, headers={"X-Console": "1"}, timeout=60)
    check("both agents enrolled", r.status_code == 200)
    keys = {a["id"]: a["api_key"] for a in requests.get(f"{BRIDGE}/v1/bridge/keys", headers=RUNTIME, timeout=30).json()["agents"]}
    analyst, writer = keys.get("data-analyst-01"), keys.get("report-writer-01")
    check("each agent has its own API key", bool(analyst) and bool(writer) and analyst != writer)

    print("--- Access")
    check("model call without a key is refused", chat("wrong").status_code == 401)
    check("tool call without a key is refused", mcp("wrong", "tools/list").status_code == 401)
    check("the key list needs the operator's credential",
          requests.get(f"{BRIDGE}/v1/bridge/keys", headers={"Authorization": f"Bearer {analyst}"}, timeout=30).status_code == 401)

    print("--- Model path, in the OpenAI chat format")
    r = chat(analyst, tools=("database_read", "file_read", "data_export"))
    body = r.json()
    calls = (body.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    check("an allowed model call is answered in the OpenAI format",
          r.status_code == 200 and body.get("object") == "chat.completion" and bool(calls),
          f"HTTP {r.status_code}, asks for {[c['function']['name'] for c in calls]}")
    runs = requests.get(f"{BRIDGE}/v1/runs", headers=RUNTIME, timeout=30).json()["runs"]
    run = requests.get(f"{BRIDGE}/v1/runs/{runs[0]['id']}", headers=RUNTIME, timeout=30).json()
    check("the gateway hid the tool outside the analyst's role from the model",
          run["steps"][-1].get("tools_removed") == ["data_export"], str(run["steps"][-1].get("tools_removed")))
    check("the workflow's calls appear as a task in the console", run["id"].startswith("flow-") and run["agent_id"] == "data-analyst-01")

    r = chat(analyst, tools=("database_read",), stream=True, stream_options={"include_usage": True})
    lines = [line[6:] for line in r.iter_lines(decode_unicode=True) if line and line.startswith("data: ")]
    chunks = [json.loads(line) for line in lines if line != "[DONE]"]
    streamed = [c for chunk in chunks for choice in chunk["choices"] for c in choice["delta"].get("tool_calls", [])]
    check("a streamed call arrives as server-sent events and ends properly",
          r.headers.get("content-type", "").startswith("text/event-stream") and lines[-1] == "[DONE]"
          and bool(streamed) and any(ch["choices"] and ch["choices"][0].get("finish_reason") for ch in chunks),
          f"{len(chunks)} chunks")
    r = chat(writer, model="mock-model")
    check("a model outside the agent's role is refused, with the reason",
          r.status_code == 403 and r.json()["error"]["code"] == "model_not_permitted", str(r.json().get("error", {}).get("code")))
    if os.environ.get("LOCAL_MODEL_TEST", "0") == "1":
        r = chat(analyst, model="local-model", text="Reply with the single word: ready", max_tokens=32)
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
        check("the local model answers through the gateway", r.status_code == 200 and bool(content),
              f"HTTP {r.status_code}, {str(content)[:60]!r} {r.json().get('error', '')}")
    else:
        print("--- Local model: not tested (set LOCAL_MODEL_TEST=1 to include it)")

    print("--- Tool path, in the Model Context Protocol")
    r = mcp(analyst, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "e2e", "version": "1"}})
    check("the tool server introduces itself", r.json().get("result", {}).get("protocolVersion") == "2025-03-26"
          and "tools" in r.json()["result"]["capabilities"])
    check("a notification needs no answer", mcp(analyst, "notifications/initialized", ident=None).status_code == 202)
    names = [t["name"] for t in mcp(analyst, "tools/list").json()["result"]["tools"]]
    check("the analyst's tools are listed", names == ["database_read", "file_read", "data_export"], str(names))
    failed, text = call(analyst, "database_read", {"table": "sales", "limit": 3})
    check("a permitted read returns rows", failed is False and "revenue_sgd" in text, text[:70])
    failed, text = call(analyst, "data_export", {"dataset": "sales"})
    check("a tool outside the analyst's role is refused", failed is True and "not permitted" in text, text[:70])
    failed, text = call(analyst, "database_read", {"table": "sales"}, **{"X-Purpose": "market_research"})
    check("a tool outside the declared purpose is refused", failed is True and "refused by policy" in text, text[:90])

    print("--- A held call: the reviewer answers while the workflow waits")
    before = {h["hold_reference"] for h in holds()}
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(zip(("failed", "text"),
                              call(writer, "data_export", {"dataset": "q4_report", "format": "pdf"}))))
    worker.start()
    new = []
    for _ in range(30):
        new = [h for h in holds() if h["hold_reference"] not in before]
        if new:
            break
        time.sleep(0.5)
    check("the export is held and reaches the review queue", len(new) == 1, f"{len(new)} new")
    if new:
        decide(new[0]["hold_reference"], True)
    worker.join(timeout=60)
    check("once approved, the waiting call runs and returns", outcome.get("failed") is False, str(outcome.get("text"))[:70])

    print("--- A held call: the reviewer answers after the workflow stopped waiting")
    arguments = {"dataset": "q4_report_final", "format": "pdf"}
    before = {h["hold_reference"] for h in holds()}
    failed, text = call(writer, "data_export", arguments, **{"X-Hold-Wait": "1"})
    check("the workflow is told the call is waiting and has not run", failed is True and "held for review" in text, text[:60])
    new = [h for h in holds() if h["hold_reference"] not in before]
    failed, text = call(writer, "data_export", arguments, **{"X-Hold-Wait": "1"})
    again = [h for h in holds() if h["hold_reference"] not in before]
    check("repeating the call does not create a second hold", len(new) == 1 and len(again) == 1, f"{len(again)} in the queue")
    if new:
        decide(new[0]["hold_reference"], True)
    failed, text = call(writer, "data_export", arguments, **{"X-Hold-Wait": "1"})
    check("after approval, repeating the call runs it", failed is False, text[:70])

    arguments = {"dataset": "q4_report_draft", "format": "pdf"}
    before = {h["hold_reference"] for h in holds()}
    call(writer, "data_export", arguments, **{"X-Hold-Wait": "1"})
    new = [h for h in holds() if h["hold_reference"] not in before]
    if new:
        decide(new[0]["hold_reference"], False)
    failed, text = call(writer, "data_export", arguments, **{"X-Hold-Wait": "1"})
    check("after rejection, repeating the call is refused", failed is True and "hold_rejected" in text, text[:70])

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
