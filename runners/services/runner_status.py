"""A runner's live status for pages, and its push to the status groups.

The state rule itself (``MachineState``, ``runner_machine_state``, the device
rule) lives in the leaf ``runners.machine_state`` so ``projects.services`` can
use it without a cycle; this module adds the loaded and pushed forms.

Groups (``runners.channels``): ``runner_status_{id}`` (run card, setup chat
header, runner detail) and ``runner_owner_{user_id}`` (the owner's list). The
event type is ``runner.status`` (``RunnerStatusEvent``). System info is sent
only to these groups, whose consumers check ``runner_access.can_view`` /
ownership on connect.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final, TypedDict

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from accounts.models import CustomUser
from projects.runner_update_offer import (
    RUNNER_UPDATE_OFFER_FIELDS,
    RunnerUpdatePayload,
    get_runner_update_offer,
)
from runners.channels import (
    RUNNER_STATUS_EVENT,
    runner_owner_group,
    runner_status_group,
)
from runners.machine_state import MachineState, runner_machine_state
from runners.models import TestRunner
from runners.services.runner_access import can_manage, can_view, can_view_by_ids
from runners.types import RunnerId
from visual_engine.services.pool_status import get_visual_engine_status
from visual_engine.types import VisualEngineStatus

logger = logging.getLogger(__name__)


class RunnerStatusEvent(TypedDict):
    type: str
    runner_id: int
    name: str
    connected: bool
    # A ``MachineState`` value.
    machine_state: str
    system_info: dict[str, Any]
    client_version: str
    capabilities: list[str]
    last_connected_at: str | None
    # The phone or tablet of a phone runner (blank / None for a desktop one).
    device_state: str
    device_detail: str
    device_reported_at: str | None


def load_runner_machine_state(
    runner_id: RunnerId, visual_engine: VisualEngineStatus | None = None
) -> MachineState:
    """The runner's state (one query, plus the engine status when not given).

    A missing or archived runner reads DISCONNECTED.
    """
    runner = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .only("connected", "device_state", "device_reported_at")
        .first()
    )
    return runner_machine_state(runner, visual_engine or get_visual_engine_status())


# What ``build_runner_status_event`` and ``runner_machine_state`` read (the
# owner's load adds ``RUNNER_UPDATE_OFFER_FIELDS``): never the token fields.
_STATUS_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "owner_id",
    "name",
    "connected",
    "system_info",
    "client_version",
    "capabilities",
    "last_connected_at",
    "device_state",
    "device_detail",
    "device_reported_at",
)


@dataclass(frozen=True)
class RunnerStatusViewer:
    """A viewer the status socket of one runner accepted."""

    owner_id: int
    # Owner-only extras: the machine details and the self-update panel.
    is_owner: bool


def authorize_runner_status_viewer(
    user: CustomUser, runner_id: RunnerId
) -> RunnerStatusViewer | None:
    """None when ``user`` may not watch the runner (``runner_access.can_view``);
    two queries at most."""
    runner = (
        TestRunner.objects.filter(pk=runner_id)
        .only("id", "owner_id", "archived_at")
        .first()
    )
    if runner is None or not can_view(user, runner):
        return None
    return RunnerStatusViewer(
        owner_id=runner.owner_id, is_owner=can_manage(user, runner)
    )


def runner_status_viewer_allowed(
    user: CustomUser, runner_id: RunnerId, viewer: RunnerStatusViewer
) -> bool:
    """Whether an accepted viewer may still watch (before each relay).

    The owner stays allowed (no query); anyone else is re-checked with
    ``can_view``, so a project member stops once the run that held the
    runner ends or moves to another runner (one query, none for a superuser).
    """
    if viewer.is_owner:
        return True
    return can_view_by_ids(user, runner_id=runner_id, owner_id=viewer.owner_id)


@dataclass(frozen=True)
class RunnerViewerState:
    status: RunnerStatusEvent
    # The self-update panel; the owner's only.
    update: RunnerUpdatePayload | None


def load_runner_status_for_viewer(
    runner_id: RunnerId, viewer: RunnerStatusViewer, visual_engine: VisualEngineStatus
) -> RunnerViewerState | None:
    """The runner's status as this viewer sees it, plus the update panel for
    the owner (one query, plus ``get_runner_update_offer``'s for the owner;
    None when the runner is gone)."""
    fields = (
        _STATUS_FIELDS + RUNNER_UPDATE_OFFER_FIELDS
        if viewer.is_owner
        else _STATUS_FIELDS
    )
    runner = TestRunner.objects.filter(pk=runner_id).only(*fields).first()
    if runner is None:
        return None
    status = status_event_for_viewer(
        build_runner_status_event(runner, visual_engine), viewer
    )
    update = get_runner_update_offer(runner).payload if viewer.is_owner else None
    return RunnerViewerState(status=status, update=update)


def status_event_for_viewer(
    event: RunnerStatusEvent, viewer: RunnerStatusViewer
) -> RunnerStatusEvent:
    """The event without the machine details for anyone but the owner: other
    viewers (members of a project running on it, superusers) see the name and
    the badge only."""
    if viewer.is_owner:
        return event
    return {
        **event,
        "system_info": {},
        "client_version": "",
        "capabilities": [],
        "device_detail": "",
    }


def build_runner_status_event(
    runner: TestRunner, visual_engine: VisualEngineStatus
) -> RunnerStatusEvent:
    return {
        "type": RUNNER_STATUS_EVENT,
        "runner_id": runner.pk,
        "name": runner.name,
        "connected": runner.connected,
        "machine_state": runner_machine_state(runner, visual_engine).value,
        "system_info": runner.system_info,
        "client_version": runner.client_version,
        "capabilities": list(runner.capabilities or []),
        "last_connected_at": (
            runner.last_connected_at.isoformat() if runner.last_connected_at else None
        ),
        "device_state": runner.device_state,
        "device_detail": runner.device_detail,
        "device_reported_at": (
            runner.device_reported_at.isoformat() if runner.device_reported_at else None
        ),
    }


def broadcast_runner_status(runner: TestRunner) -> None:
    """Push the runner's state to its status group and its owner's group.

    Never raises: a dead channel layer must not fail a connect or disconnect.
    """
    layer: Any = get_channel_layer()
    if layer is None:
        return
    try:
        event = build_runner_status_event(runner, get_visual_engine_status())
        async_to_sync(layer.group_send)(runner_status_group(runner.pk), event)
        async_to_sync(layer.group_send)(runner_owner_group(runner.owner_id), event)
    except Exception:
        logger.exception("Runner status broadcast failed for runner %s", runner.pk)
