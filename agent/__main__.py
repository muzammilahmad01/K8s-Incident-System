"""CLI entrypoint.

Two run modes (our deviation from the SRS's poll-only design, for easier
dev/demo):

    incident-agent once   [--alert-file fixtures/crashloop.json]
    incident-agent watch  [--interval 60]
    incident-agent load-runbooks [--reset]

`once` runs a single graph cycle and exits — used for development and the demo.
`watch` runs the FR-1 poll loop. `load-runbooks` (re)indexes the runbook corpus.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import typer
from rich import print as rprint
from rich.json import JSON

from agent.config import get_settings
from agent.graph import build_graph

app = typer.Typer(add_completion=False, help="K8s Incident Response Agent")


def _run_cycle(initial_state: dict) -> dict:
    graph = build_graph()
    thread_id = initial_state.get("incident_id") or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    final = graph.invoke(initial_state, config=config)
    return final


def _emit(final: dict) -> None:
    """Print the report (CLI pretty) and append it to the log file (FR-7)."""
    settings = get_settings()
    report = final.get("report", {})
    rprint(JSON(json.dumps(report)))
    with open(settings.report_log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(report) + "\n")


@app.command()
def once(
    alert_file: Path = typer.Option(
        None, help="JSON file with a list of alert objects to inject (skips Alertmanager)."
    ),
) -> None:
    """Run a single incident-response cycle and exit."""
    initial: dict = {"incident_id": str(uuid.uuid4())}
    if alert_file:
        initial["alerts"] = json.loads(alert_file.read_text())
    final = _run_cycle(initial)
    _emit(final)


@app.command()
def watch(interval: int = typer.Option(None, help="Poll interval seconds (default from config).")) -> None:
    """Continuously poll Alertmanager and respond to firing alerts (FR-1)."""
    settings = get_settings()
    period = interval or settings.poll_interval_seconds
    rprint(f"[green]Watching[/green] {settings.alertmanager_url} every {period}s. Ctrl-C to stop.")
    while True:
        try:
            final = _run_cycle({"incident_id": str(uuid.uuid4())})
            if final.get("alerts"):
                _emit(final)
            else:
                rprint("[dim]No firing alerts.[/dim]")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            rprint(f"[red]cycle error:[/red] {exc}")
        time.sleep(period)


@app.command("load-runbooks")
def load_runbooks_cmd(reset: bool = typer.Option(False, help="Delete and rebuild the collection.")) -> None:
    """Index the runbook Markdown corpus into Chroma."""
    from agent.memory import load_runbooks

    n = load_runbooks(reset=reset)
    rprint(f"[green]Indexed[/green] {n} runbook chunks.")


if __name__ == "__main__":
    app()
