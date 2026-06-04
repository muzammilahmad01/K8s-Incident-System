"""The 10 LangChain tools (SRS §4).

Each tool is a thin, well-typed wrapper around ``kubectl``, the Prometheus HTTP
API, or a Chroma similarity search. Tools return plain Python data (dicts /
lists / strings) so they are easy to unit-test with mocked subprocess / HTTP.

Design notes:
* kubectl is invoked via subprocess (works identically locally and in-cluster).
  We deliberately do NOT use the Python kubernetes client here to keep tool
  output shaped exactly like what an engineer would see at the terminal.
* The mutating tools (restart_pod, scale_deployment) are still ordinary tools;
  the *safety policy* (only LOW severity, only allowlisted actions) is enforced
  by the graph in nodes.py / decide_action, never by these functions alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

import httpx
from langchain_core.tools import tool

from agent.config import get_settings

# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


class KubectlError(RuntimeError):
    """Raised when a kubectl invocation fails."""


def _run_kubectl(args: list[str], timeout: int = 20) -> str:
    """Run ``kubectl <args>`` and return stdout. Raises KubectlError on failure."""
    binary = shutil.which("kubectl")
    if binary is None:
        raise KubectlError("kubectl not found on PATH")
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
        raise KubectlError(f"kubectl timed out: {' '.join(args)}") from exc
    if proc.returncode != 0:
        raise KubectlError(proc.stderr.strip() or f"kubectl failed: {' '.join(args)}")
    return proc.stdout


def _parse_json(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise KubectlError(f"could not parse kubectl JSON output: {exc}") from exc


# ----------------------------------------------------------------------------
# Read-only kubectl tools
# ----------------------------------------------------------------------------


@tool
def get_pod_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Get the status of a single pod (phase, container states, restart counts).

    Wraps ``kubectl get pod <name> -n <namespace> -o json`` and returns a
    compact status summary.
    """
    raw = _run_kubectl(["get", "pod", name, "-n", namespace, "-o", "json"])
    pod = _parse_json(raw)
    status = pod.get("status", {})
    containers = []
    for cs in status.get("containerStatuses", []) or []:
        state = next(iter(cs.get("state", {})), "unknown")
        containers.append(
            {
                "name": cs.get("name"),
                "ready": cs.get("ready"),
                "restartCount": cs.get("restartCount"),
                "state": state,
                "reason": (cs.get("state", {}).get(state, {}) or {}).get("reason"),
            }
        )
    return {
        "name": pod.get("metadata", {}).get("name", name),
        "namespace": namespace,
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "containers": containers,
    }


@tool
def describe_pod(name: str, namespace: str = "default") -> str:
    """Describe a pod and return the most recent events (last ~10 lines).

    Wraps ``kubectl describe pod`` and extracts the Events section, which is
    where scheduling failures, image pull errors, and OOM kills show up.
    """
    raw = _run_kubectl(["describe", "pod", name, "-n", namespace])
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Events:"):
            event_lines = [ln for ln in lines[i + 1 :] if ln.strip()]
            return "\n".join(event_lines[-10:]) or "No events."
    return "No events section found."


@tool
def get_pod_logs(name: str, namespace: str = "default", previous: bool = False) -> str:
    """Get the last 50 log lines of a pod. Set previous=True for a crashed container.

    Wraps ``kubectl logs <name> --tail=50 [--previous]``.
    """
    args = ["logs", name, "-n", namespace, "--tail=50"]
    if previous:
        args.append("--previous")
    try:
        return _run_kubectl(args)
    except KubectlError as exc:
        # --previous fails if there is no prior container instance; fall back.
        if previous:
            return _run_kubectl(["logs", name, "-n", namespace, "--tail=50"])
        raise exc


@tool
def get_node_conditions() -> list[dict[str, Any]]:
    """Get per-node conditions (Ready, MemoryPressure, DiskPressure, PIDPressure).

    Wraps ``kubectl get nodes -o json``. Used for node-level alerts.
    """
    raw = _run_kubectl(["get", "nodes", "-o", "json"])
    data = _parse_json(raw)
    out: list[dict[str, Any]] = []
    for node in data.get("items", []):
        out.append(
            {
                "name": node.get("metadata", {}).get("name"),
                "conditions": [
                    {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
                    for c in node.get("status", {}).get("conditions", [])
                ],
            }
        )
    return out


@tool
def get_deployment_status(name: str, namespace: str = "default") -> dict[str, Any]:
    """Get a deployment's replica counts and conditions.

    Wraps ``kubectl get deployment <name> -o json``.
    """
    raw = _run_kubectl(["get", "deployment", name, "-n", namespace, "-o", "json"])
    dep = _parse_json(raw)
    status = dep.get("status", {})
    spec = dep.get("spec", {})
    return {
        "name": dep.get("metadata", {}).get("name", name),
        "namespace": namespace,
        "desired_replicas": spec.get("replicas"),
        "ready_replicas": status.get("readyReplicas", 0),
        "available_replicas": status.get("availableReplicas", 0),
        "conditions": [
            {"type": c.get("type"), "status": c.get("status"), "reason": c.get("reason")}
            for c in status.get("conditions", [])
        ],
    }


# ----------------------------------------------------------------------------
# Prometheus tool
# ----------------------------------------------------------------------------


@tool
def query_prometheus(promql: str) -> dict[str, Any]:
    """Run an instant PromQL query against Prometheus and return the raw result.

    Wraps ``GET {prometheus_url}/api/v1/query?query=<promql>``.
    """
    settings = get_settings()
    url = f"{settings.prometheus_url}/api/v1/query"
    resp = httpx.get(url, params={"query": promql}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("data", {})


# ----------------------------------------------------------------------------
# Mutating (remediation) tools — SAFE only via graph policy, not on their own
# ----------------------------------------------------------------------------


@tool
def restart_pod(name: str, namespace: str = "default") -> dict[str, Any]:
    """Restart a pod by deleting it (its controller recreates it). SAFE action.

    Wraps ``kubectl delete pod <name>``. The graph only calls this for LOW
    severity incidents whose action is on the safe_actions allowlist.
    """
    out = _run_kubectl(["delete", "pod", name, "-n", namespace])
    return {"action": "restart_pod", "target": f"{namespace}/{name}", "result": out.strip()}


@tool
def scale_deployment(name: str, replicas: int, namespace: str = "default") -> dict[str, Any]:
    """Scale a deployment to a given replica count. SAFE action.

    Wraps ``kubectl scale deployment <name> --replicas=<n>``.
    """
    out = _run_kubectl(
        ["scale", "deployment", name, f"--replicas={replicas}", "-n", namespace]
    )
    return {
        "action": "scale_deployment",
        "target": f"{namespace}/{name}",
        "replicas": replicas,
        "result": out.strip(),
    }


# ----------------------------------------------------------------------------
# Chroma retrieval tools (implemented against memory.py)
# ----------------------------------------------------------------------------


@tool
def search_runbook(query: str, top_k: int = 3) -> list[str]:
    """Similarity-search the runbook vector store; return the top-k chunks."""
    from agent.memory import search_runbooks

    return search_runbooks(query, top_k=top_k)


@tool
def search_past_incidents(query: str, top_k: int = 3) -> list[str]:
    """Similarity-search long-term incident memory; return similar past incidents."""
    from agent.memory import search_incidents

    return search_incidents(query, top_k=top_k)


# Convenience groupings used by nodes/graph.
READ_TOOLS = [
    get_pod_status,
    describe_pod,
    get_pod_logs,
    get_node_conditions,
    get_deployment_status,
    query_prometheus,
    search_runbook,
    search_past_incidents,
]
SAFE_ACTION_TOOLS = {
    "restart_pod": restart_pod,
    "scale_deployment": scale_deployment,
}
ALL_TOOLS = READ_TOOLS + list(SAFE_ACTION_TOOLS.values())
