"""What machine readiness needs from the Test Runner and from Machine setup.

Design (plan D2: ``machine_readiness`` depends only on ``projects.models``,
the knowledge base services and the agents' LLM client): the runner actions
(``projects.services.controller_*``), the setup-turn gate
(``machine_setup.turn_state``), the runner-update gate
(``projects.runner_update_state``) and the run gates
(``projects.run_machine_use``) are injected as a ``RunnerPorts`` value.
The composition root is outside this app: ``projects.machine_readiness_ports
.build_runner_ports()``, called by the ``projects`` views and Celery tasks
that scan or check the machine, and by ``machine_setup``'s readiness tools.
Celery cannot carry callables, so the tasks that need runner calls live in
``projects.tasks`` (names in ``machine_readiness.task_names``). The ports may
raise ``projects.exceptions`` controller errors, a leaf module of exception
classes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from controller_client.protocol import ClientCapability
from machine_readiness.types import InventoryEntry


@dataclass(frozen=True)
class DirectoryScanResult:
    """A ``scan_directory`` reply; ``error`` set means nothing was listed."""

    root: str
    entries: tuple[InventoryEntry, ...]
    truncated: bool
    permission_errors: int
    error: str


@dataclass(frozen=True)
class PathStat:
    """An ``inspect_paths`` reply item, in request order."""

    path: str
    exists: bool
    is_dir: bool


@dataclass(frozen=True)
class AppCheckResult:
    """A ``check_app_installed`` reply: ``message`` starts with INSTALLED or
    NOT INSTALLED when ``success``."""

    success: bool
    message: str


class ScanDirectoryPort(Protocol):
    def __call__(
        self,
        project_id: int,
        path: str,
        *,
        max_depth: int,
        max_entries: int,
        allow_network: bool,
        timeout: float,
    ) -> DirectoryScanResult: ...


class InspectPathsPort(Protocol):
    def __call__(
        self,
        project_id: int,
        paths: Sequence[str],
        *,
        allow_network: bool,
        timeout: float,
    ) -> list[PathStat]: ...


class CheckAppInstalledPort(Protocol):
    def __call__(
        self, project_id: int, app_name: str, *, timeout: float
    ) -> AppCheckResult: ...


class ProjectGatePort(Protocol):
    def __call__(self, project_id: int, now: datetime) -> bool: ...


class ProjectCheckPort(Protocol):
    def __call__(self, project_id: int) -> bool: ...


@dataclass(frozen=True)
class RunnerPorts:
    """Runner actions and gates, built by ``projects.machine_readiness_ports``."""

    scan_directory: ScanDirectoryPort
    inspect_paths: InspectPathsPort
    check_app_installed: CheckAppInstalledPort
    # A queued or running Machine setup turn of the project.
    live_turn_exists: ProjectGatePort
    # A Test Runner self-update in flight.
    runner_update_in_progress: ProjectGatePort
    # A Started run that is not Manual (the agent drives the machine).
    agent_run_started: ProjectCheckPort
    # A tester performing a claimed, unpaused case of a Started Manual run.
    manual_tester_on_machine: ProjectCheckPort


def advertises(capabilities: object, capability: ClientCapability) -> bool:
    """Whether a runner's ``Project.agent_capabilities`` list has ``capability``."""
    return isinstance(capabilities, list) and capability.value in capabilities
