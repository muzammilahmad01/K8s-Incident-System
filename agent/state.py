"""Shared graph state for the K8s Incident Response Agent.

Every LangGraph node receives and returns (a partial update to) this
``IncidentState``. It mirrors SRS §3.3, with two additions:

* ``confidence`` — required by FR-4 alongside root_cause/severity/actions.
* ``needs_human_approval`` / ``incident_id`` — bookkeeping the graph needs
  to implement the human-in-the-loop pause (FR-5) and persistence (FR-6).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Severity = Literal["LOW", "MEDIUM", "HIGH"]


class IncidentState(TypedDict, total=False):
    # --- ingestion (poll_alerts) ---
    alerts: list[dict[str, Any]]          # raw Prometheus/Alertmanager alert objects

    # --- investigation (fetch_cluster_state) ---
    cluster_state: dict[str, Any]         # kubectl output per affected resource

    # --- retrieval (retrieve_runbook) ---
    runbook_context: list[str]            # retrieved runbook chunks
    past_incidents: list[str]             # retrieved from long-term memory

    # --- reasoning (reason) ---
    root_cause: str                       # LLM reasoning output
    severity: Severity                    # LOW | MEDIUM | HIGH
    recommended_actions: list[str]
    confidence: float                     # 0.0 - 1.0 (FR-4)

    # --- decision + remediation (decide_action, execute_remediation) ---
    needs_human_approval: bool            # set True for MEDIUM/HIGH (FR-5)
    actions_taken: list[str]

    # --- output (persist_incident, report) ---
    incident_id: str                      # stable id used for memory + report
    report: dict[str, Any]
