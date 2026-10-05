"""Acceptance test for the LangGraph workflow. Uses the scripted model: no provider key, no local model.

It runs the analyst-to-writer workflow twice. A second thread plays the reviewer
through the console: it approves the writer's export the first time and rejects it the second.
"""
import asyncio
import os
import sys
import threading
import time

import httpx

from research_ops import run

CONSOLE = os.environ.get("CONSOLE_URL", "http://console:8088")
AUTH = ("workflow.reviewer", os.environ["CONSOLE_PASSWORD"])
TASK = "Report the Q4 sales figures by region and send the report out."
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def reviewer(approve: bool, seen: list):
    with httpx.Client(auth=AUTH, timeout=30) as http:
        known = {h["hold_reference"] for h in http.get(f"{CONSOLE}/api/overview").json()["holds"]}
        for _ in range(240):
            new = [h for h in http.get(f"{CONSOLE}/api/overview").json()["holds"] if h["hold_reference"] not in known]
            if new:
                seen.append(new[0])
                http.post(f"{CONSOLE}/api/holds/{new[0]['hold_reference']}/decision", json={"approve": approve},
                          headers={"X-Console": "1"})
                return
            time.sleep(0.5)


async def attempt(approve: bool):
    seen: list = []
    thread = threading.Thread(target=reviewer, args=(approve, seen))
    thread.start()
    await asyncio.sleep(1)       # let the reviewer note the holds that already exist
    state = await run(TASK, "scripted-model", "scripted-model")
    thread.join(timeout=30)
    return state, seen


async def main():
    with httpx.Client(auth=AUTH, timeout=60) as http:
        r = http.post(f"{CONSOLE}/api/enrol", json={}, headers={"X-Console": "1"})
    check("both agents enrolled", r.status_code == 200)

    print("--- Workflow with the export approved")
    state, seen = await attempt(True)
    steps = state.get("steps", [])
    asked = [(s["agent"], s["asked_for"]) for s in steps if "asked_for" in s]
    check("the analyst used only the tools its role allows",
          [tool for agent, tool in asked if agent == "analyst"] == ["database_read", "file_read"], str(asked))
    check("the analyst's findings reached the writer", bool(state.get("findings")) and bool(state.get("report")))
    check("the writer asked for the report and the export, and never for the delete tool",
          [tool for agent, tool in asked if agent == "writer"] == ["report_generation", "data_export"], str(asked))
    check("the export was held for the reviewer", len(seen) == 1 and seen[0]["tool"] == "data_export"
          and seen[0]["agent_id"] == "report-writer-01", str([h.get("tool") for h in seen]))
    export = [s for s in steps if s.get("tool") == "data_export"]
    check("once approved, the export ran", bool(export) and export[-1]["status"] != "error" and "accepted" in export[-1]["returned"],
          export[-1]["returned"][:60] if export else "no export step")

    print("--- Workflow with the export rejected")
    state, seen = await attempt(False)
    export = [s for s in state.get("steps", []) if s.get("tool") == "data_export"]
    check("once rejected, the export did not run and the writer was told",
          bool(export) and export[-1]["status"] == "error" and "hold_rejected" in export[-1]["returned"],
          export[-1]["returned"][:70] if export else "no export step")
    check("the workflow still finished", bool(state.get("report")))
    check("both agents worked under one task identifier", state["run_id"].startswith("wf-"), state["run_id"])

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
