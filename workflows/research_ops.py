"""Research Operations workflow in LangGraph: a data analyst hands its findings to a report writer.

Each agent is a separate identity with its own API key. Every model call and
every tool call goes to the bridge, and from there through the policy gateway.
The workflow holds no tool credential and has no other route.

    analyst  -->  (findings?)  -->  writer  -->  end
                       |
                       +--> end, if the analyst found nothing

Choose the model for each agent: a model inside the organisation (local-model),
the external provider (claude-haiku), or the scripted stand-in (scripted-model).

Usage
  research_ops.py "Report Q4 sales by region and send the report out"
                  [--model local-model] [--analyst-model ...] [--writer-model ...] [--json]
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
import warnings
from typing import TypedDict

warnings.filterwarnings("ignore", message=".*create_react_agent.*")

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import create_react_agent

BRIDGE = os.environ.get("BRIDGE_URL", "http://agent-runtime:9070").rstrip("/")
AGENTS = {
    "analyst": {
        "key_env": "AGENT_KEY_DATA_ANALYST_01", "purpose": "sales_reporting",
        "brief": "You are the data analyst of a research operations team. Use the tools you are given to gather "
                 "the figures the task needs, then answer briefly with what you found.",
    },
    "writer": {
        "key_env": "AGENT_KEY_REPORT_WRITER_01", "purpose": "report_delivery",
        "brief": "You are the report writer of a research operations team. Use the tools you are given to produce "
                 "the report from the findings and send it out, then confirm briefly what you did. If a tool call "
                 "is refused or is waiting for review, say so plainly and do not claim it was done.",
    },
}


class State(TypedDict, total=False):
    task: str
    run_id: str
    models: dict
    findings: str
    report: str
    steps: list


def plain(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in content)
    return str(content or "")


async def run_agent(name: str, model: str, task: str, run_id: str) -> tuple[str, list]:
    """One agent, under its own identity. Returns its answer and what it did."""
    spec = AGENTS[name]
    key = os.environ[spec["key_env"]]
    context = {"X-Purpose": spec["purpose"], "X-Task-Id": run_id}     # one task id for the whole workflow run
    headers = {"Authorization": f"Bearer {key}"} | context
    llm = ChatOpenAI(model=model, base_url=f"{BRIDGE}/v1", api_key=key, max_retries=0, timeout=180,
                     default_headers=context)
    client = MultiServerMCPClient({"gateway": {"url": f"{BRIDGE}/mcp", "transport": "streamable_http", "headers": headers}})
    tools = await client.get_tools()
    for tool in tools:
        tool.handle_tool_error = True       # a refusal is information for the model, not a crash
    agent = create_react_agent(llm, tools, prompt=spec["brief"])
    steps = []
    try:
        result = await agent.ainvoke({"messages": [("user", task)]}, {"recursion_limit": 16})
    except Exception as exc:  # noqa: BLE001  for example the gateway refusing the model call
        detail = str(exc)
        return "", [{"agent": name, "model": model, "stopped": detail[:300]}]
    for message in result["messages"]:
        for call in getattr(message, "tool_calls", None) or []:
            steps.append({"agent": name, "asked_for": call["name"], "arguments": call["args"]})
        if message.type == "tool":
            steps.append({"agent": name, "tool": message.name, "status": getattr(message, "status", ""),
                          "returned": plain(message.content)[:240]})
    return plain(result["messages"][-1].content), steps


async def analyst(state: State) -> State:
    findings, steps = await run_agent("analyst", state["models"]["analyst"], state["task"], state["run_id"])
    return {"findings": findings, "steps": state.get("steps", []) + steps}


async def writer(state: State) -> State:
    task = f"{state['task']}\n\nFindings from the data analyst:\n{state['findings']}"
    report, steps = await run_agent("writer", state["models"]["writer"], task, state["run_id"])
    return {"report": report, "steps": state["steps"] + steps}


def after_analyst(state: State) -> str:
    return "writer" if state.get("findings") else END


def build():
    graph = StateGraph(State)
    graph.add_node("analyst", analyst)
    graph.add_node("writer", writer)
    graph.add_edge(START, "analyst")
    graph.add_conditional_edges("analyst", after_analyst, {"writer": "writer", END: END})
    graph.add_edge("writer", END)
    return graph.compile()


async def run(task: str, analyst_model: str, writer_model: str) -> State:
    return await build().ainvoke({"task": task, "run_id": f"wf-{uuid.uuid4().hex[:10]}", "models": {"analyst": analyst_model, "writer": writer_model}, "steps": []})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task")
    parser.add_argument("--model", default=os.environ.get("WORKFLOW_MODEL", "local-model"))
    parser.add_argument("--analyst-model")
    parser.add_argument("--writer-model")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    models = {"analyst": args.analyst_model or args.model, "writer": args.writer_model or args.model}
    state = asyncio.run(run(args.task, models["analyst"], models["writer"]))
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(f"Task: {args.task}  (run {state['run_id']})")
    print(f"Models: analyst on {models['analyst']}, writer on {models['writer']}\n")
    for step in state.get("steps", []):
        if "asked_for" in step:
            print(f"  [{step['agent']}] asks for {step['asked_for']} {json.dumps(step['arguments'])}")
        elif "tool" in step:
            print(f"  [{step['agent']}] {step['tool']} returned{' (refused or waiting)' if step['status'] == 'error' else ''}: {step['returned']}")
        else:
            print(f"  [{step['agent']}] stopped: {step['stopped']}")
    print(f"\nAnalyst's findings:\n{state.get('findings') or '(none)'}")
    print(f"\nWriter's report:\n{state.get('report') or '(the writer did not run or gave no answer)'}")
    print(f"\nSee each step in the console. Its ledger record: audit_trail.py --task {state['run_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
