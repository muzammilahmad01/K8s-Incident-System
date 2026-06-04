"""Unit tests for the LangChain tools (mocked kubectl / HTTP — no live cluster)."""

from __future__ import annotations

import json

import pytest

from agent import tools


def test_get_pod_status_parses_container_state(mocker):
    pod_json = json.dumps(
        {
            "metadata": {"name": "crash-test"},
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "app",
                        "ready": False,
                        "restartCount": 7,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    }
                ],
            },
        }
    )
    mocker.patch.object(tools, "_run_kubectl", return_value=pod_json)

    result = tools.get_pod_status.invoke({"name": "crash-test", "namespace": "default"})

    assert result["phase"] == "Running"
    assert result["containers"][0]["reason"] == "CrashLoopBackOff"
    assert result["containers"][0]["restartCount"] == 7


def test_describe_pod_extracts_events(mocker):
    described = "Name: crash-test\nEvents:\n  Warning  BackOff  pod restarting\n"
    mocker.patch.object(tools, "_run_kubectl", return_value=described)

    out = tools.describe_pod.invoke({"name": "crash-test"})

    assert "BackOff" in out


def test_get_pod_logs_falls_back_when_no_previous(mocker):
    calls = []

    def fake(args, timeout=20):
        calls.append(args)
        if "--previous" in args:
            raise tools.KubectlError("previous terminated container not found")
        return "log line 1\nlog line 2"

    mocker.patch.object(tools, "_run_kubectl", side_effect=fake)

    out = tools.get_pod_logs.invoke({"name": "crash-test", "previous": True})

    assert "log line 1" in out
    assert len(calls) == 2  # tried --previous, then fell back


def test_scale_deployment_builds_command(mocker):
    run = mocker.patch.object(tools, "_run_kubectl", return_value="scaled")
    result = tools.scale_deployment.invoke({"name": "web", "replicas": 3})
    assert result["replicas"] == 3
    args = run.call_args[0][0]
    assert "--replicas=3" in args


def test_run_kubectl_raises_when_binary_missing(mocker):
    mocker.patch.object(tools.shutil, "which", return_value=None)
    with pytest.raises(tools.KubectlError):
        tools._run_kubectl(["get", "pods"])
