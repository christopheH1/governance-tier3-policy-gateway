"""Summarise what the OpenTelemetry Collector has recorded for the policy gateway.

Reads the collector's output files directly. Standard library only.

  python telemetry_summary.py --dir /telemetry            summary and the most recent traces
  python telemetry_summary.py --dir /telemetry --check    exit 1 unless telemetry looks complete

Remember that traces are sampled: every denied, referred, or failed request is
kept, and about one in ten allowed requests. The metrics count every request.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict


def attributes(items):
    out = {}
    for item in items or []:
        value = item.get("value", {})
        if "arrayValue" in value:
            out[item["key"]] = [next(iter(v.values()), None) for v in value["arrayValue"].get("values", [])]
        else:
            out[item["key"]] = next(iter(value.values()), None)
    return out


def read_lines(pattern):
    for path in sorted(glob.glob(pattern), key=os.path.getmtime):
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue          # a line still being written


def load_traces(directory):
    traces = defaultdict(lambda: {"root": None, "stages": {}})
    for record in read_lines(os.path.join(directory, "traces*.jsonl")):
        for resource in record.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    duration = (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6
                    entry = traces[span["traceId"]]
                    if span["name"].startswith("stage."):
                        name = span["name"][6:]
                        entry["stages"][name] = entry["stages"].get(name, 0.0) + duration
                    else:
                        entry["root"] = {"name": span["name"], "start": int(span["startTimeUnixNano"]),
                                         "duration": duration, **attributes(span.get("attributes"))}
    return {trace_id: entry for trace_id, entry in traces.items() if entry["root"]}


def load_decision_counts(directory):
    """The counter is cumulative, so the latest value for each label set is the total so far."""
    latest = {}
    for record in read_lines(os.path.join(directory, "metrics*.jsonl")):
        for resource in record.get("resourceMetrics", []):
            for scope in resource.get("scopeMetrics", []):
                for metric in scope.get("metrics", []):
                    if metric.get("name") != "governance.decisions":
                        continue
                    for point in metric.get("sum", {}).get("dataPoints", []):
                        labels = attributes(point.get("attributes"))
                        key = (labels.get("path"), labels.get("outcome"), labels.get("mode") or "")
                        value = int(point.get("asInt", point.get("asDouble", 0)))
                        start = point.get("startTimeUnixNano")
                        # a gateway restart begins a new series: add the finished one to the total
                        previous = latest.get(key)
                        if previous and previous["start"] != start:
                            latest[key] = {"start": start, "value": value, "carried": previous["carried"] + previous["value"]}
                        else:
                            latest[key] = {"start": start, "value": value, "carried": previous["carried"] if previous else 0}
    return {key: entry["value"] + entry["carried"] for key, entry in latest.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default="/telemetry")
    parser.add_argument("--last", type=int, default=12, help="how many recent traces to list")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    traces = load_traces(args.dir)
    counts = load_decision_counts(args.dir)

    print("Requests counted by the gateway (metrics, every request):")
    if not counts:
        print("  none recorded yet (metrics are sent every 10 seconds)")
    for (path, outcome, mode), value in sorted(counts.items(), key=lambda item: str(item[0])):
        print(f"  {path or '?':6} {outcome or '?':6} {mode:5} {value:>6}")

    kept = Counter((entry["root"].get("governance.path"), entry["root"].get("governance.outcome"))
                   for entry in traces.values())
    print(f"\nTraces kept after sampling: {len(traces)}")
    for (path, outcome), value in sorted(kept.items(), key=lambda item: str(item[0])):
        print(f"  {path or '?':6} {outcome or '?':6} {value:>6}")

    recent = sorted(traces.items(), key=lambda item: item[1]["root"]["start"])[-args.last:]
    if recent:
        print(f"\nMost recent {len(recent)} traces:")
    for trace_id, entry in recent:
        root = entry["root"]
        subject = root.get("tool.name") or root.get("model.name") or ""
        reasons = ",".join(root.get("governance.reasons") or [])
        stages = "  ".join(f"{name}={value:.1f}" for name, value in entry["stages"].items())
        print(f"  {trace_id[:12]}  {root.get('governance.path', '?'):5} {root.get('governance.outcome', '?'):5} "
              f"{root.get('governance.mode') or '':4} {subject:18} {root.get('agent.id', ''):26} "
              f"{root['duration']:7.1f} ms  [{stages}]" + (f"  {reasons}" if reasons else ""))

    if args.check:
        problems = []
        if not counts:
            problems.append("no decision counts in the metrics")
        denied = [entry for entry in traces.values() if entry["root"].get("governance.outcome") == "deny"]
        if not denied:
            problems.append("no denied request among the traces")
        elif not any(entry["stages"] for entry in denied):
            problems.append("denied traces carry no stage timings")
        if not any(entry["root"].get("agent.id") for entry in traces.values()):
            problems.append("no trace names an agent")
        leaked = [key for entry in traces.values() for key in entry["root"] if "param" in key or "prompt" in key or "message" in key]
        if leaked:
            problems.append(f"trace attributes that should not be there: {sorted(set(leaked))}")
        print("\nCHECK: " + ("telemetry looks complete" if not problems else "; ".join(problems)))
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
