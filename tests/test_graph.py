"""Tests for node logic, especially the safety-critical decide_action routing."""

from __future__ import annotations

from agent import nodes
from agent.config import Settings


def test_poll_alerts_dedupes_injected(settings, crashloop_alert):
    dup = dict(crashloop_alert)
    out = nodes.poll_alerts({"alerts": [crashloop_alert, dup]}, settings)
    assert len(out["alerts"]) == 1


# --- decide_action: the core safety gate (NFR: enforced in graph, not prompt) ---


def test_low_severity_with_safe_action_executes(settings):
    state = {
        "severity": "LOW",
        "recommended_actions": ["restart_pod to clear the crash loop"],
    }
    assert nodes.decide_action(state, settings) == "execute_remediation"


def test_high_severity_never_executes(settings):
    state = {
        "severity": "HIGH",
        "recommended_actions": ["restart_pod"],  # even with a safe action named
    }
    assert nodes.decide_action(state, settings) == "persist"


def test_medium_severity_never_executes(settings):
    state = {"severity": "MEDIUM", "recommended_actions": ["restart_pod"]}
    assert nodes.decide_action(state, settings) == "persist"


def test_low_severity_without_safe_action_pauses(settings):
    state = {
        "severity": "LOW",
        "recommended_actions": ["edit the deployment memory limit"],  # not allowlisted
    }
    assert nodes.decide_action(state, settings) == "persist"


def test_kill_switch_disables_automation():
    s = Settings(enable_auto_remediation=False)
    state = {"severity": "LOW", "recommended_actions": ["restart_pod"]}
    assert nodes.decide_action(state, s) == "persist"


def test_set_approval_flag_matches_decision(settings):
    low = {"severity": "LOW", "recommended_actions": ["restart_pod"]}
    high = {"severity": "HIGH", "recommended_actions": ["restart_pod"]}
    assert nodes.set_approval_flag(low, settings)["needs_human_approval"] is False
    assert nodes.set_approval_flag(high, settings)["needs_human_approval"] is True


# --- reason JSON parsing robustness ---


def test_parse_llm_json_handles_code_fence():
    raw = '```json\n{"root_cause": "x", "severity": "LOW", "recommended_actions": [], "confidence": 0.9}\n```'
    parsed = nodes._parse_llm_json(raw)
    assert parsed["severity"] == "LOW"


def test_normalize_severity_defaults_to_medium():
    assert nodes._normalize_severity("garbage") == "MEDIUM"
    assert nodes._normalize_severity("high") == "HIGH"


def test_report_shape(settings):
    state = {
        "alerts": [{"labels": {"alertname": "KubePodCrashLooping"}}],
        "severity": "LOW",
        "confidence": 0.8,
        "root_cause": "test pod crashes on exit 1",
        "recommended_actions": ["restart_pod"],
        "actions_taken": ["restart_pod -> done"],
    }
    report = nodes.report(state)["report"]
    assert report["alert_name"] == "KubePodCrashLooping"
    assert report["severity"] == "LOW"
    assert report["actions_taken"]
