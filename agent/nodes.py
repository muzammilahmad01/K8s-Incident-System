r"""The 8 LangGraph node functions (SRS §3.2).

Each node takes the current ``IncidentState`` and returns a *partial* dict that
LangGraph merges into the state. Keeping returns partial makes nodes easy to
unit-test in isolation.

Flow:
    poll_alerts -> fetch_cluster_state -> retrieve_runbook -> reason
                -> decide_action -(conditional)-> execute_remediation -> persist_incident -> report
                                              \-> persist_incident -> report   (human pause)
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent.config import Settings, get_llm, get_settings
from agent.state import IncidentState
from agent.tools import (
    KubectlError,
    describe_pod,
    get_deployment_status,
    get_node_conditions,
    get_pod_logs,
    get_pod_status,
    restart_pod,
    scale_deployment,
)

NODE_LEVEL_KEYWORDS = ("node", "memorypressure", "diskpressure", "kubelet")


# ----------------------------------------------------------------------------
# 1. poll_alerts (FR-1)
# ----------------------------------------------------------------------------


def poll_alerts(state: IncidentState, settings: Settings | None = None) -> dict[str, Any]:
    """Query Alertmanager, keep only firing/active alerts, dedupe within cycle.

    If ``state['alerts']`` is already populated (e.g. a fixture/injected alert
    in --once mode), we trust it and skip the HTTP call.
    """
    s = settings or get_settings()
    if state.get("alerts"):
        return {"alerts": _dedupe_alerts(state["alerts"])}

    resp = httpx.get(f"{s.alertmanager_url}/api/v2/alerts", timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    firing = [
        a
        for a in raw
        if a.get("status", {}).get("state") == "active"
        and not a.get("status", {}).get("silencedBy")
        and not a.get("status", {}).get("inhibitedBy")
    ]
    return {"alerts": _dedupe_alerts(firing)}


def _dedupe_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    out = []
    for a in alerts:
        labels = a.get("labels", {})
        key = (labels.get("alertname"), labels.get("namespace"), labels.get("pod"))
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


# ----------------------------------------------------------------------------
# 2. fetch_cluster_state (FR-2)
# ----------------------------------------------------------------------------


def fetch_cluster_state(state: IncidentState) -> dict[str, Any]:
    """For each firing alert, gather pod status/events/logs (and node conditions)."""
    cluster_state: dict[str, Any] = {}
    for alert in state.get("alerts", []):
        labels = alert.get("labels", {})
        alertname = labels.get("alertname", "unknown")
        namespace = labels.get("namespace", "default")
        pod = labels.get("pod") or labels.get("instance")

        entry: dict[str, Any] = {"alertname": alertname, "namespace": namespace}

        if pod:
            entry["pod"] = pod
            entry["status"] = _safe(lambda: get_pod_status.invoke({"name": pod, "namespace": namespace}))
            entry["events"] = _safe(lambda: describe_pod.invoke({"name": pod, "namespace": namespace}))
            entry["logs"] = _safe(
                lambda: get_pod_logs.invoke({"name": pod, "namespace": namespace, "previous": True})
            )

        if any(k in alertname.lower() for k in NODE_LEVEL_KEYWORDS):
            entry["nodes"] = _safe(lambda: get_node_conditions.invoke({}))

        if "deployment" in labels:
            entry["deployment"] = _safe(
                lambda: get_deployment_status.invoke(
                    {"name": labels["deployment"], "namespace": namespace}
                )
            )

        cluster_state[alertname] = entry
    return {"cluster_state": cluster_state}


def _safe(fn) -> Any:
    """Run a tool call, returning an error string instead of raising.

    Cluster collection is best-effort: a missing pod or kubectl hiccup should
    degrade gracefully into the report, not crash the whole graph.
    """
    try:
        return fn()
    except (KubectlError, Exception) as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ----------------------------------------------------------------------------
# 3. retrieve_runbook (FR-3)
# ----------------------------------------------------------------------------


def retrieve_runbook(state: IncidentState) -> dict[str, Any]:
    """Retrieve top-k runbook chunks + similar past incidents for the alerts."""
    from agent.memory import search_incidents, search_runbooks

    s = get_settings()
    query = _retrieval_query(state)
    return {
        "runbook_context": search_runbooks(query, top_k=s.retrieval_top_k),
        "past_incidents": search_incidents(query, top_k=s.retrieval_top_k),
    }


def _retrieval_query(state: IncidentState) -> str:
    parts = []
    for alert in state.get("alerts", []):
        labels = alert.get("labels", {})
        parts.append(labels.get("alertname", ""))
        if labels.get("reason"):
            parts.append(labels["reason"])
    # Fold in any container waiting reasons captured from cluster state.
    for entry in state.get("cluster_state", {}).values():
        status = entry.get("status")
        if isinstance(status, dict):
            for c in status.get("containers", []) or []:
                if c.get("reason"):
                    parts.append(c["reason"])
    return " ".join(p for p in parts if p) or "kubernetes incident"


# ----------------------------------------------------------------------------
# 4. reason (FR-4) — single structured LLM call
# ----------------------------------------------------------------------------


_SYSTEM_PROMPT = """You are an expert Kubernetes SRE triaging an incident.
Given the firing alert(s), live cluster state, relevant runbook excerpts, and
similar past incidents, produce a concise root-cause analysis.

Severity guidance:
- LOW: isolated, self-recoverable (e.g. a single crashing test pod, transient restart).
- MEDIUM: affects a real workload or could escalate; needs human judgement.
- HIGH: data loss risk, node-level, or multi-service impact.

Respond ONLY with a JSON object matching this schema:
{
  "root_cause": string,                  // one or two sentences
  "severity": "LOW" | "MEDIUM" | "HIGH",
  "recommended_actions": [string, ...],  // imperative steps, most important first
  "confidence": number                   // 0.0 - 1.0
}"""


def reason(state: IncidentState, llm=None) -> dict[str, Any]:
    """Single structured LLM call producing root_cause/severity/actions/confidence."""
    llm = llm or get_llm()
    user_payload = {
        "alerts": [a.get("labels", {}) for a in state.get("alerts", [])],
        "cluster_state": state.get("cluster_state", {}),
        "runbook_context": state.get("runbook_context", []),
        "past_incidents": state.get("past_incidents", []),
    }
    messages = [
        ("system", _SYSTEM_PROMPT),
        ("human", json.dumps(user_payload, default=str)[:12000]),
    ]
    raw = llm.invoke(messages)
    content = raw.content if hasattr(raw, "content") else str(raw)
    parsed = _parse_llm_json(content)
    return {
        "root_cause": parsed.get("root_cause", "Unknown"),
        "severity": _normalize_severity(parsed.get("severity")),
        "recommended_actions": parsed.get("recommended_actions", []),
        "confidence": float(parsed.get("confidence", 0.0) or 0.0),
    }


def _parse_llm_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{") :] if "{" in content else content
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _normalize_severity(value: Any) -> str:
    v = str(value or "").upper()
    return v if v in ("LOW", "MEDIUM", "HIGH") else "MEDIUM"


# ----------------------------------------------------------------------------
# 5. decide_action (FR-5) — conditional routing function
# ----------------------------------------------------------------------------


def decide_action(state: IncidentState, settings: Settings | None = None) -> str:
    """Routing function for the conditional edge.

    Returns the name of the next node: "execute_remediation" or "persist".
    SAFETY (NFR): automated action only when ALL hold:
      * auto-remediation enabled
      * severity == LOW
      * at least one recommended action maps to a safe, allowlisted tool
    Otherwise we flag for human approval and skip remediation.
    """
    s = settings or get_settings()
    severity = state.get("severity", "MEDIUM")
    if (
        s.enable_auto_remediation
        and severity == "LOW"
        and _matched_safe_action(state, s) is not None
    ):
        return "execute_remediation"
    return "persist"


def _matched_safe_action(state: IncidentState, s: Settings) -> str | None:
    """Find the first recommended action that names an allowlisted safe tool."""
    for action in state.get("recommended_actions", []):
        text = action.lower()
        for safe in s.safe_actions:
            if safe.replace("_", " ") in text or safe in text:
                return safe
    return None


def set_approval_flag(state: IncidentState, settings: Settings | None = None) -> dict[str, Any]:
    """Node that records whether the incident is awaiting human approval.

    Runs before the conditional edge so the flag is in state for the report.
    """
    s = settings or get_settings()
    auto = decide_action(state, s) == "execute_remediation"
    return {"needs_human_approval": not auto}


# ----------------------------------------------------------------------------
# 6. execute_remediation (FR-5)
# ----------------------------------------------------------------------------


def execute_remediation(state: IncidentState, settings: Settings | None = None) -> dict[str, Any]:
    """Execute the matched safe action, then verify the resulting status."""
    s = settings or get_settings()
    safe = _matched_safe_action(state, s)
    actions_taken = list(state.get("actions_taken", []))

    target = _primary_target(state)
    if safe is None or target is None:
        return {"actions_taken": actions_taken}

    name, namespace = target
    try:
        if safe == "restart_pod":
            result = restart_pod.invoke({"name": name, "namespace": namespace})
            verify = _safe(lambda: get_pod_status.invoke({"name": name, "namespace": namespace}))
        elif safe == "scale_deployment":
            result = scale_deployment.invoke({"name": name, "replicas": 1, "namespace": namespace})
            verify = _safe(
                lambda: get_deployment_status.invoke({"name": name, "namespace": namespace})
            )
        else:  # pragma: no cover - guarded by allowlist
            return {"actions_taken": actions_taken}
        actions_taken.append(f"{safe} -> {result.get('result', 'done')} | verified: {verify}")
    except (KubectlError, Exception) as exc:  # noqa: BLE001
        actions_taken.append(f"{safe} FAILED: {exc}")
    return {"actions_taken": actions_taken}


def _primary_target(state: IncidentState) -> tuple[str, str] | None:
    """Pick the (name, namespace) the safe action should operate on."""
    for entry in state.get("cluster_state", {}).values():
        if entry.get("pod"):
            return entry["pod"], entry.get("namespace", "default")
    for alert in state.get("alerts", []):
        labels = alert.get("labels", {})
        if labels.get("pod"):
            return labels["pod"], labels.get("namespace", "default")
    return None


# ----------------------------------------------------------------------------
# 7. persist_incident (FR-6)
# ----------------------------------------------------------------------------


def persist_incident(state: IncidentState, timestamp: str | None = None) -> dict[str, Any]:
    """Write the resolved incident to long-term Chroma memory."""
    from agent.memory import persist_incident as _persist

    alert_name = next(
        (a.get("labels", {}).get("alertname") for a in state.get("alerts", [])), "unknown"
    )
    incident_id = state.get("incident_id") or f"{alert_name}-{timestamp or 'now'}"
    record = {
        "incident_id": incident_id,
        "alert_name": alert_name,
        "root_cause": state.get("root_cause"),
        "actions_taken": state.get("actions_taken", []),
        "outcome": "auto-remediated"
        if state.get("actions_taken")
        else ("awaiting-approval" if state.get("needs_human_approval") else "analyzed"),
        "severity": state.get("severity"),
        "timestamp": timestamp or "now",
    }
    _persist(record)
    return {"incident_id": incident_id}


# ----------------------------------------------------------------------------
# 8. report (FR-7)
# ----------------------------------------------------------------------------


def report(state: IncidentState) -> dict[str, Any]:
    """Assemble the final structured JSON incident report."""
    alert_name = next(
        (a.get("labels", {}).get("alertname") for a in state.get("alerts", [])), "unknown"
    )
    report_obj = {
        "incident_id": state.get("incident_id"),
        "alert_name": alert_name,
        "severity": state.get("severity"),
        "confidence": state.get("confidence"),
        "root_cause": state.get("root_cause"),
        "recommended_actions": state.get("recommended_actions", []),
        "actions_taken": state.get("actions_taken", []),
        "needs_human_approval": state.get("needs_human_approval", False),
        "runbook_used": bool(state.get("runbook_context")),
        "past_incidents_considered": len(state.get("past_incidents", [])),
    }
    return {"report": report_obj}
