"""What machine readiness needs from the Test Runner and from Machine setup.

Design (plan D2: ``machine_readiness`` depends only on ``projects.models``,
the knowledge base services and the agents' LLM client): the runner actions
(``projects.services.controller_*``) and the "is the runner busy" gate
(``projects.runner_use`` and ``machine_setup.turn_state``, per Test Runner)
are injected as a ``RunnerPorts`` value. Every port takes the ``RunnerId``
of the Test Runner to act on.
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
from enum import StrEnum
from typing import Protocol

from controller_client.protocol import ClientCapability
from machine_readiness.types import InventoryEntry
from runners.types import RunnerId


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


class RunnerBusy(StrEnum):
    """Why a runner may not be used by a scan or a check now."""

    # The runner no longer serves the project (archived, or its owner is no
    # longer an active writable member).
    NOT_SERVING = "not_serving"

    # A Started agent-driven run on the runner.
    RUN_STARTED = "run_started"
    # A claimed, unpaused Manual case on the runner.
    MANUAL_TESTER = "manual_tester"
    # A live Machine setup turn drives the runner (not the asking one).
    SETUP_RUNNING = "setup_running"
    # A self-update of the runner is in flight.
    UPDATING = "updating"
    # A paused run of another project holds the runner.
    OTHER_PROJECT = "other_project"
    # The other readiness work holds the runner: a scan while checking, or
    # a check while scanning (one readiness action per runner at a time).
    SCANNING = "scanning"
    CHECKING = "checking"


class ReadinessWork(StrEnum):
    """Which readiness action asks ``runner_busy``: its own lock does not
    count against it, the other one does."""

    SCAN = "scan"
    CHECK = "check"


class ScanDirectoryPort(Protocol):
    def __call__(
        self,
        runner_id: RunnerId,
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
        runner_id: RunnerId,
        paths: Sequence[str],
        *,
        allow_network: bool,
        timeout: float,
    ) -> list[PathStat]: ...


class CheckAppInstalledPort(Protocol):
    def __call__(
        self, runner_id: RunnerId, app_name: str, *, timeout: float
    ) -> AppCheckResult: ...


class RunnerBusyPort(Protocol):
    def __call__(
        self,
        runner_id: RunnerId,
        project_id: int,
        *,
        work: ReadinessWork,
        ignore_turn_id: int | None,
    ) -> RunnerBusy | None: ...


@dataclass(frozen=True)
class RunnerPorts:
    """Runner actions and gates, built by ``projects.machine_readiness_ports``."""

    scan_directory: ScanDirectoryPort
    inspect_paths: InspectPathsPort
    check_app_installed: CheckAppInstalledPort
    # Why the runner is busy for a scan or check (``work``) of ``project_id``
    # (None when free); ``ignore_turn_id`` is the asking setup turn.
    runner_busy: RunnerBusyPort


def advertises(capabilities: object, capability: ClientCapability) -> bool:
    """Whether a runner's ``TestRunner.capabilities`` list has ``capability``."""
    return isinstance(capabilities, list) and capability.value in capabilities
