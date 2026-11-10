"""How a Test Runner is shown on project pages (pure: no queries).

A runner of another user is always named "name (owner)" (Q1). Runner names,
hostnames and owner names are plain text: templates escape them.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import CustomUser
from runners.machine_state import runner_machine_state
from runners.models import TestRunner
from visual_engine.types import VisualEngineStatus


@dataclass(frozen=True)
class RunnerSummary:
    """One runner as a project page shows it."""

    runner_id: int
    # "name (owner)".
    label: str
    # A ``runners.machine_state.MachineState`` value (badge_machine.html).
    machine_state: str
    # The connected machine's hostname ("" when unknown); machine-supplied.
    hostname: str


def owner_label(owner: CustomUser) -> str:
    return owner.get_full_name().strip() or owner.email


def runner_label(runner: TestRunner) -> str:
    """ "name (owner)"; ``runner.owner`` must be loaded (select_related)."""
    return f"{runner.name} ({owner_label(runner.owner)})"


def summarize_runner(
    runner: TestRunner, visual_engine: VisualEngineStatus
) -> RunnerSummary:
    """``runner.owner`` must be loaded; reads only loaded columns."""
    system_info = runner.system_info if isinstance(runner.system_info, dict) else {}
    hostname = system_info.get("hostname")
    return RunnerSummary(
        runner_id=runner.pk,
        label=runner_label(runner),
        machine_state=runner_machine_state(runner, visual_engine).value,
        hostname=hostname if isinstance(hostname, str) else "",
    )
