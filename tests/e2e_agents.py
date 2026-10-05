"""Agent acceptance test: the Research Operations scenario, driven through the console.

Uses the scripted model, so it needs no provider key and costs nothing. The test
acts as the operator and the reviewer would, through the same console API the web
page uses.

The exit code is non-zero if any check fails.
"""
import os
import sys
import time

import requests

CONSOLE = os.environ.get("CONSOLE_URL", "http://console:8088")
AUTH = ("e2e.reviewer", os.environ["CONSOLE_PASSWORD"])
REQUESTER = ("e2e.requester", os.environ["CONSOLE_PASSWORD"])     # a second person, with the requester role only
CHANGE = {"X-Console": "1"}
VESTIGIA = os.environ.get("VESTIGIA_URL", "http://vestigia:8002")


def ledger_people(agent_id, run_id):
    """Who the ledger says each model call of this task was made for, in order."""
    for _ in range(10):
        resp = requests.get(f"{VESTIGIA}/events", timeout=30, params={"actor_id": agent_id, "action_type": "MODEL_DECISION", "limit": 100},
                            headers={"Authorization": f"Bearer {os.environ['VESTIGIA_API_KEY']}"})
        if resp.status_code != 429:
            break
        time.sleep(0.5)
    mine = [e for e in resp.json().get("events", []) if (e.get("evidence", {}).get("intent") or {}).get("task_id") == run_id]
    mine.sort(key=lambda e: e.get("timestamp", ""))
    return [e["evidence"]["intent"].get("on_behalf_of") for e in mine]
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def get(path):
    return requests.get(f"{CONSOLE}{path}", auth=AUTH, timeout=30)


def post(path, payload, **kwargs):
    return requests.post(f"{CONSOLE}{path}", json=payload, auth=kwargs.pop("auth", AUTH),
                         headers=kwargs.pop("headers", CHANGE), timeout=30)


def start(agent_id, purpose, task):
    resp = post("/api/runs", {"agent_id": agent_id, "purpose": purpose, "model": "scripted-model", "task": task})
    return resp.json().get("id")


def wait_for_run(run_id, until, seconds=60):
    deadline = time.time() + seconds
    run = {}
    while time.time() < deadline:
        run = get(f"/api/runs/{run_id}").json()
        if until(run):
            break
        time.sleep(1)
    return run


def steps(run, kind, title=None):
    return [s for s in run.get("steps", []) if s["kind"] == kind and (title is None or s["title"] == title)]


def held_for(run_id):
    holds = get("/api/overview").json().get("holds", [])
    return next((h for h in holds if h.get("intent", {}).get("task_id") == run_id), None)


def main():
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if requests.get(f"{CONSOLE}/health", timeout=3).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(2)

    print("--- Console access")
    check("page needs a sign-in", requests.get(f"{CONSOLE}/", timeout=10).status_code == 401)
    check("wrong password is refused",
          requests.get(f"{CONSOLE}/api/overview", auth=("someone", "wrong"), timeout=10).status_code == 401)
    r = post("/api/enrol", {}, headers={})
    check("a change that did not come from the console page is refused", r.status_code == 403, f"HTTP {r.status_code}")
    check("page loads when signed in", get("/").status_code == 200 and "agent console" in get("/").text)
    check("the tests' password does not let any other name in",
          requests.get(f"{CONSOLE}/api/overview", auth=("someone", AUTH[1]), timeout=10).status_code == 401)
    r = post("/api/enrol", {}, auth=REQUESTER)
    check("an account without the operator role cannot enrol agents", r.status_code == 403, f"HTTP {r.status_code}")

    print("--- Operator: enrol the agents")
    r = post("/api/enrol", {})
    enrolled = {a["id"]: a["enrolled"] for a in r.json().get("agents", [])}
    check("both agents enrolled", enrolled == {"data-analyst-01": True, "report-writer-01": True}, str(enrolled))
    agents = {a["id"]: a for a in get("/api/overview").json()["agents"]}
    check("the analyst may hold tokens for its role's tools only",
          "database_read" in agents["data-analyst-01"]["allowed_tools"]
          and "data_export" not in agents["data-analyst-01"]["allowed_tools"])

    print("--- Data analyst: a task that needs two reads")
    run = wait_for_run(start("data-analyst-01", "sales_reporting", "Report the Q4 sales figures by region."),
                       lambda r: r.get("status") in ("finished", "stopped", "failed"))
    first = steps(run, "model")[0] if steps(run, "model") else {}
    check("the run finished with an answer", run.get("status") == "finished" and bool(run.get("answer")), run.get("status"))
    check("the gateway hid the tool the analyst may not use from the model",
          first.get("tools_removed") == ["data_export"] and "data_export" not in first.get("tools_offered", []),
          str(first.get("tools_removed")))
    reads = steps(run, "tool", "database_read")
    check("the database read was allowed and returned figures",
          bool(reads) and reads[0]["outcome"] == "allow" and "revenue_sgd" in str(reads[0].get("result")))
    check("every step carries a trace identifier",
          all(s.get("trace_id") for s in run.get("steps", []) if s["kind"] in ("model", "tool")))

    print("--- Data analyst: a purpose that does not fit the tools")
    run = wait_for_run(start("data-analyst-01", "market_research", "Look up the Q4 sales figures."),
                       lambda r: r.get("status") in ("finished", "stopped", "failed"))
    denied = steps(run, "tool", "database_read")
    offered = (steps(run, "model") or [{}])[0].get("tools_offered", [])
    check("a tool outside the declared purpose is not offered or is denied",
          "database_read" not in offered or (bool(denied) and denied[0]["outcome"] == "deny"), f"offered: {offered}")

    print("--- Report writer: an export that needs a reviewer")
    run_id = start("report-writer-01", "report_delivery", "Produce the Q4 market research report and send it out.")
    run = wait_for_run(run_id, lambda r: r.get("status") == "waiting for review")
    check("the run pauses for review", run.get("status") == "waiting for review", run.get("status"))
    check("the report was generated before the hold",
          any(s["outcome"] == "allow" for s in steps(run, "tool", "report_generation")))
    first = (steps(run, "model") or [{}])[0]
    check("the delete tool the writer knows about was hidden from the model",
          "database_delete" in first.get("tools_removed", []), str(first.get("tools_removed")))
    hold = held_for(run_id)
    check("the held export appears in the review queue", hold is not None and hold.get("tool") == "data_export")
    check("the review queue names the person who asked", (hold or {}).get("intent", {}).get("on_behalf_of") == "e2e.reviewer",
          str((hold or {}).get("intent", {}).get("on_behalf_of")))
    r = post(f"/api/holds/{hold['hold_reference']}/decision", {"approve": True}, auth=REQUESTER) if hold else None
    check("an account without the reviewer role cannot approve", r is not None and r.status_code == 403,
          f"HTTP {getattr(r, 'status_code', None)}")
    r = post(f"/api/holds/{hold['hold_reference']}/decision", {"approve": True}) if hold else None
    check("the reviewer approves it", r is not None and r.status_code == 200 and r.json().get("status") == "approved")
    run = wait_for_run(run_id, lambda r: r.get("status") in ("finished", "stopped", "failed"))
    export = steps(run, "tool", "data_export")
    check("the export then runs and the agent finishes",
          run.get("status") == "finished" and bool(export) and export[0]["outcome"] == "allow", run.get("status"))

    print("--- Report writer: an export the reviewer rejects")
    run_id = start("report-writer-01", "report_delivery", "Send the Q4 report to the external mailing list.")
    wait_for_run(run_id, lambda r: r.get("status") == "waiting for review")
    hold = held_for(run_id)
    r = post(f"/api/holds/{hold['hold_reference']}/decision", {"approve": False}) if hold else None
    run = wait_for_run(run_id, lambda r: r.get("status") in ("finished", "stopped", "failed"))
    export = steps(run, "tool", "data_export")
    check("a rejected export does not run, and the agent is told",
          bool(export) and export[0]["outcome"] == "deny" and "hold_rejected" in export[0].get("reasons", []),
          str(export[0].get("reasons")) if export else "no export step")

    print("--- A person replies to the agent")
    run_id = start("data-analyst-01", "sales_reporting", "Report the Q4 sales figures by region.")
    run = wait_for_run(run_id, lambda r: r.get("status") in ("finished", "stopped", "failed"))
    before = len(steps(run, "model"))
    check("the task records who started it", run.get("requested_by") == "e2e.reviewer", str(run.get("requested_by")))
    people = ledger_people("data-analyst-01", run_id)
    check("the ledger names that person on every model call of the task",
          bool(people) and set(people) == {"e2e.reviewer"}, str(people))
    r = post(f"/api/runs/{run_id}/reply", {"text": "Thank you. Which region was highest?"}, auth=REQUESTER)
    check("a reply to a finished task is accepted", r.status_code == 200 and run.get("can_reply") is True, f"HTTP {r.status_code}")
    time.sleep(0.5)
    run = wait_for_run(run_id, lambda r: r.get("status") in ("finished", "stopped", "failed"))
    said = steps(run, "person")
    check("the reply is shown with the name of the person who sent it",
          bool(said) and said[0]["title"].startswith("e2e.requester") and "highest" in said[0].get("text", ""))
    people = ledger_people("data-analyst-01", run_id)
    check("after a reply from someone else, the ledger names that person on what follows",
          len(people) >= 2 and people[0] == "e2e.reviewer" and people[-1] == "e2e.requester", str(people))
    check("the agent answers again, through the gateway, within the same task",
          run.get("status") == "finished" and len(steps(run, "model")) > before and bool(run.get("answer"))
          and all(step.get("trace_id") for step in steps(run, "model")), f"{len(steps(run, 'model'))} model calls")
    r = post("/api/runs/no-such-run/reply", {"text": "hello"})
    check("a reply to an unknown task is refused", r.status_code == 404, f"HTTP {r.status_code}")

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
