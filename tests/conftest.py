"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Default settings with auto-remediation on; safe for unit tests."""
    return Settings(enable_auto_remediation=True)


@pytest.fixture
def crashloop_alert() -> dict:
    return {
        "status": {"state": "active"},
        "labels": {
            "alertname": "KubePodCrashLooping",
            "namespace": "default",
            "pod": "crash-test",
            "reason": "CrashLoopBackOff",
        },
    }
