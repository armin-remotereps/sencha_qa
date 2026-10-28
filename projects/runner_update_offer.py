"""Use case: what the Connection card offers about updating the Test Runner.

``get_runner_update_offer`` builds the card's state (template context and the
``json_script`` initial state); ``broadcast_runner_update`` pushes the same
payload to the Environment page's ``AgentStatusConsumer`` whenever an update
changes. The gates shared with ``runner_update_request`` live here too, so
the card lists exactly what the request would refuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone
from machine_setup.turn_state import MACHINE_SETUP_RUNNING_MESSAGE, live_turn_exists
from projects.models import (
    Project,
    RunnerUpdate,
    RunnerUpdateStatus,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.runner_update_state import (
    expire_stale_runner_updates,
    runner_update_timeout,
)

from controller_client.protocol import (
    CLIENT_VERSION,
    ClientCapability,
    UpdatePhase,
    version_tuple,
)

RUN_STARTED_MESSAGE: Final[str] = (
    "A test run is in progress on this machine. Wait for it to finish or stop it."
)
AWAITING_PERSON_MESSAGE: Final[str] = (
    "A case is waiting for a person's step on this machine. Finish that step first."
)

PHASE_LABELS: Final[dict[UpdatePhase, str]] = {
    UpdatePhase.ACCEPTED: "Accepted by the Test Runner",
    UpdatePhase.PREPARING: "Checking and unpacking the new version",
    UpdatePhase.CREATING_ENVIRONMENT: "Creating the Python environment",
    UpdatePhase.INSTALLING_DEPENDENCIES: "Installing dependencies",
    UpdatePhase.INSTALLING_BROWSERS: "Installing browsers",
    UpdatePhase.STARTING: "Starting the new Test Runner",
    UpdatePhase.WAITING_FOR_NEW_RUNNER: "Waiting for the new Test Runner to connect",
    UpdatePhase.FAILED: "Failed",
    UpdatePhase.OLD_REMOVED: "Old Test Runner folder removed",
    UpdatePhase.OLD_REMOVAL_FAILED: "Old Test Runner folder could not be removed",
}


class RunnerUpdateOfferState(StrEnum):
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    # Older, but too old to update itself (no SELF_UPDATE_V1).
    MANUAL_REQUIRED = "manual_required"
    # Could update, but the machine is busy; ``blockers`` says why.
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    # Could update; the latest attempt at this server version failed.
    LAST_FAILED = "last_failed"
    # Could update; the latest attempt at this server version did not finish.
    LAST_EXPIRED = "last_expired"
    NOT_CONNECTED = "not_connected"


_UPDATABLE_STATES: Final[frozenset[RunnerUpdateOfferState]] = frozenset(
    {
        RunnerUpdateOfferState.AVAILABLE,
        RunnerUpdateOfferState.LAST_FAILED,
        RunnerUpdateOfferState.LAST_EXPIRED,
    }
)


class RunnerUpdateRecordPayload(TypedDict):
    id: str
    status: str
    status_label: str
    phase: str
    phase_label: str
    from_version: str
    to_version: str
    # Runner-written plain text; render escaped.
    detail: str
    old_removal: str
    in_flight: bool
    created_at: str
    finished_at: str | None


class RunnerUpdatePayload(TypedDict):
    state: str
    server_version: str
    runner_version: str
    can_update: bool
    blockers: list[str]
    latest: RunnerUpdateRecordPayload | None


@dataclass(frozen=True)
class RunnerUpdateOffer:
    state: RunnerUpdateOfferState
    server_version: str
    # Empty when no runner is connected.
    runner_version: str
    # The project's newest update (any status), or None.
    latest: RunnerUpdate | None
    # Filled for BLOCKED only.
    blockers: tuple[str, ...]

    @property
    def can_update(self) -> bool:
        return self.state in _UPDATABLE_STATES

    @property
    def in_progress(self) -> bool:
        return self.state == RunnerUpdateOfferState.IN_PROGRESS

    @property
    def payload(self) -> RunnerUpdatePayload:
        return {
            "state": self.state.value,
            "server_version": self.server_version,
            "runner_version": self.runner_version,
            "can_update": self.can_update,
            "blockers": list(self.blockers),
            "latest": _record_payload(self.latest) if self.latest else None,
        }


# ---------------------------------------------------------------------------
# Gates shared with ``runner_update_request``
# ---------------------------------------------------------------------------


def runner_is_older(runner_version: str) -> bool:
    """Whether the runner is older than the server; unparseable counts as older."""
    server = version_tuple(CLIENT_VERSION)
    runner = version_tuple(runner_version)
    if server is None:
        return False
    return runner is None or runner < server


def runner_can_self_update(capabilities: Sequence[str]) -> bool:
    return ClientCapability.SELF_UPDATE_V1 in capabilities


def list_runner_update_blockers(project_id: int, now: datetime) -> list[str]:
    """User-safe reasons the machine is busy (the in-flight update aside).

    A Paused (Needs help) run does not block: updating is how a Runner
    outdated pause is fixed.
    """
    blockers: list[str] = []
    if TestRun.objects.filter(
        project_id=project_id, status=TestRunStatus.STARTED
    ).exists():
        blockers.append(RUN_STARTED_MESSAGE)
    if live_turn_exists(project_id, now):
        blockers.append(MACHINE_SETUP_RUNNING_MESSAGE)
    if TestRunTestCase.objects.filter(
        test_run__project_id=project_id,
        status=TestRunTestCaseStatus.AWAITING_PERSON,
    ).exists():
        blockers.append(AWAITING_PERSON_MESSAGE)
    return blockers


# ---------------------------------------------------------------------------
# Offer and broadcast
# ---------------------------------------------------------------------------


def get_runner_update_offer(project: Project) -> RunnerUpdateOffer:
    """The Connection card's update state for ``project`` (its agent fields as loaded).

    One query for the newest update, plus the gate queries only when the
    runner could update. A stale in-flight update is written ``expired`` here.
    """
    now = timezone.now()
    latest = _latest_update(project.id, now)
    connected = project.agent_connected and bool(project.agent_channel_name)
    runner_version = project.agent_client_version if connected else ""

    def offer(
        state: RunnerUpdateOfferState, blockers: Sequence[str] = ()
    ) -> RunnerUpdateOffer:
        return RunnerUpdateOffer(
            state=state,
            server_version=CLIENT_VERSION,
            runner_version=runner_version,
            latest=latest,
            blockers=tuple(blockers),
        )

    if latest is not None and latest.is_in_flight:
        return offer(RunnerUpdateOfferState.IN_PROGRESS)
    if not connected:
        return offer(RunnerUpdateOfferState.NOT_CONNECTED)
    if not runner_is_older(runner_version):
        return offer(RunnerUpdateOfferState.UP_TO_DATE)
    if not runner_can_self_update(project.agent_capabilities):
        return offer(RunnerUpdateOfferState.MANUAL_REQUIRED)
    blockers = list_runner_update_blockers(project.id, now)
    if blockers:
        return offer(RunnerUpdateOfferState.BLOCKED, blockers)
    if latest is not None and latest.to_version == CLIENT_VERSION:
        if latest.status == RunnerUpdateStatus.FAILED:
            return offer(RunnerUpdateOfferState.LAST_FAILED)
        if latest.status == RunnerUpdateStatus.EXPIRED:
            return offer(RunnerUpdateOfferState.LAST_EXPIRED)
    return offer(RunnerUpdateOfferState.AVAILABLE)


def broadcast_runner_update(project_id: int) -> None:
    """Push the project's update state to its Environment page sockets.

    Sync; inside a transaction call it from ``transaction.on_commit``.
    """
    layer: Any = get_channel_layer()
    if layer is None:
        return
    project = (
        Project.objects.only(
            "id",
            "agent_connected",
            "agent_channel_name",
            "agent_client_version",
            "agent_capabilities",
        )
        .filter(pk=project_id)
        .first()
    )
    if project is None:
        return
    payload = get_runner_update_offer(project).payload
    async_to_sync(layer.group_send)(
        f"agent_status_{project_id}",
        {"type": "agent.runner_update", "runner_update": payload},
    )


def _latest_update(project_id: int, now: datetime) -> RunnerUpdate | None:
    latest = (
        RunnerUpdate.objects.filter(project_id=project_id)
        .order_by("-created_at")
        .first()
    )
    if (
        latest is not None
        and latest.is_in_flight
        and latest.created_at < now - runner_update_timeout()
    ):
        expire_stale_runner_updates(project_id, now)
        latest.refresh_from_db()
    return latest


def _record_payload(update: RunnerUpdate) -> RunnerUpdateRecordPayload:
    return {
        "id": str(update.pk),
        "status": update.status,
        "status_label": update.get_status_display(),
        "phase": update.phase,
        "phase_label": _phase_label(update.phase),
        "from_version": update.from_version,
        "to_version": update.to_version,
        "detail": update.detail,
        "old_removal": update.old_removal,
        "in_flight": update.is_in_flight,
        "created_at": update.created_at.isoformat(),
        "finished_at": update.finished_at.isoformat() if update.finished_at else None,
    }


def _phase_label(phase: str) -> str:
    try:
        return PHASE_LABELS[UpdatePhase(phase)]
    except ValueError:
        return ""
