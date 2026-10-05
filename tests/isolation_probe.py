"""Network isolation probe. Runs on the agents network only, as an agent would.

An agent must reach the policy gateway and the provisioner and nothing else:
not the identity service, the policy engine, the ledger, the key-value store,
the tools themselves, the model adapter, either model server, or the outside world.
"""
import socket
import sys

MUST_REACH = [("policy-gateway", 9090), ("provisioner", 9080)]
MUST_NOT_REACH = [("tessera", 8001), ("opa", 8181), ("vestigia", 8002), ("valkey", 6379),
                  ("mock-tools", 8000), ("model-adapter", 4000), ("otel-collector", 4318),
                  ("ollama", 11434), ("ollama-checker", 11434),
                  ("1.1.1.1", 443)]


def reachable(host, port):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True, "connected"
    except OSError as exc:
        return False, type(exc).__name__


failures = 0
for host, port in MUST_REACH:
    ok, detail = reachable(host, port)
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] agent can reach {host}:{port}  ({detail})")
for host, port in MUST_NOT_REACH:
    ok, detail = reachable(host, port)
    failures += ok
    print(f"[{'FAIL' if ok else 'PASS'}] agent cannot reach {host}:{port}  ({detail})")
total = len(MUST_REACH) + len(MUST_NOT_REACH)
print(f"\n{total - failures} of {total} checks passed")
sys.exit(1 if failures else 0)
