"""LangGraph state machine wiring (SRS §3.2).

Linear pipeline with one conditional branch at decide_action:

    poll_alerts -> fetch_cluster_state -> retrieve_runbook -> reason
        -> set_approval -> [decide_action]
              |-- LOW + safe action --> execute_remediation --> persist --> report -> END
              |-- otherwise (human-in-the-loop) -------------> persist --> report -> END

The MemorySaver checkpointer persists state so MEDIUM/HIGH incidents can pause
at the human-approval point and be resumed later (FR-5).
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agent import nodes
from agent.state import IncidentState


def build_graph(checkpointer=None):
    """Construct and compile the incident-response graph."""
    g = StateGraph(IncidentState)

    g.add_node("poll_alerts", nodes.poll_alerts)
    g.add_node("fetch_cluster_state", nodes.fetch_cluster_state)
    g.add_node("retrieve_runbook", nodes.retrieve_runbook)
    g.add_node("reason", nodes.reason)
    g.add_node("set_approval", nodes.set_approval_flag)
    g.add_node("execute_remediation", nodes.execute_remediation)
    g.add_node("persist", nodes.persist_incident)
    g.add_node("emit_report", nodes.report)

    g.set_entry_point("poll_alerts")
    g.add_edge("poll_alerts", "fetch_cluster_state")
    g.add_edge("fetch_cluster_state", "retrieve_runbook")
    g.add_edge("retrieve_runbook", "reason")
    g.add_edge("reason", "set_approval")

    # Conditional edge: decide_action returns the next node name.
    g.add_conditional_edges(
        "set_approval",
        nodes.decide_action,
        {
            "execute_remediation": "execute_remediation",
            "persist": "persist",
        },
    )
    g.add_edge("execute_remediation", "persist")
    g.add_edge("persist", "emit_report")
    g.add_edge("emit_report", END)

    return g.compile(checkpointer=checkpointer or MemorySaver())
