"""Stand-in tools for testing. Each records that it was called, so a test can
prove that a denied or held request never reached a tool."""
from collections import Counter
from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="Mock tools", docs_url=None, redoc_url=None, openapi_url=None)
calls: Counter = Counter()
SALES = [
    {"region": "APAC", "quarter": "Q4", "revenue_sgd": 4120000}, {"region": "EMEA", "quarter": "Q4", "revenue_sgd": 3310000},
    {"region": "Americas", "quarter": "Q4", "revenue_sgd": 5270000}, {"region": "ANZ", "quarter": "Q4", "revenue_sgd": 980000},
    {"region": "India", "quarter": "Q4", "revenue_sgd": 1460000},
]


@app.get("/_calls")
def call_counts():
    return dict(calls)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/{path:path}")
async def any_tool(path: str, request: Request) -> dict[str, Any]:
    calls[path] += 1
    params = await request.json()
    if path == "database/read":
        rows = min(int(params.get("limit", 10)), 5)
        return {"tool": path, "rows": [{"id": i, "table": params.get("table", "sales"), **SALES[i]} for i in range(rows)]}
    if path == "file/read":
        return {"tool": path, "path": params.get("path"), "text": "Q4 notes: APAC grew fastest. EMEA was flat. One large deal slipped to Q1."}
    if path == "report":
        return {"tool": path, "report_id": "RPT-2026-Q4-001", "title": params.get("title"), "pages": 6}
    return {"tool": path, "accepted": True, "params": params}
