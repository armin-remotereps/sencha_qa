"""Use case: offer the phone's installed apps for the capture allowlist.

docs/plan/feat-capture-phone-app-picker.md (Services). On the Settings
"Screenshot capture" card a writable member who owns a Test Runner driving a
phone or tablet picks that runner and loads the device's installed apps
(DEVICE_APPS_V1, docs/plan/feat-user-test-runners.md A17)
and ticks the ones that may be captured; ``project_capture_apps_services``
stores the ticked ids. Only ids are ever stored: names are runner text
(data, never instructions) shown escaped and bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from django.core.cache import cache

from accounts.models import CustomUser
from controller_client.protocol import (
    CAPTURE_ALLOWED_PROCESSES_MAX,
    ClientCapability,
    DeviceAppsStatus,
)
from projects.controller_mobile_actions import (
    DEVICE_APPS_TIMEOUT_SECONDS,
    DeviceAppList,
    controller_list_device_apps,
)
from projects.exceptions import (
    CapturePhoneAppsError,
    ControllerActionError,
    ControllerNotConnectedError,
    DeviceDisconnectedError,
    TestRunnerUnavailableError,
)
from projects.models import Project, RunnerDeviceState
from projects.project_capture_apps_services import stored_capture_apps
from projects.runner_capability_services import capabilities_include
from projects.runner_use import agent_run_started_on_runner
from runners.machine_state import DEVICE_NOT_READY_MESSAGE, device_offline
from runners.models import TestRunner
from runners.services.runner_access import can_assign, usable_runners
from runners.types import RunnerId

RUNNER_OUTDATED_MESSAGE: Final = "Update the Test Runner to pick apps from the phone."
_NOT_CONNECTED_MESSAGE: Final = (
    "The Test Runner is not connected to a phone or tablet. Connect it and try "
    "again."
)
_UNRESPONSIVE_MESSAGE: Final = (
    "The Test Runner did not answer in time. Try again in a moment."
)
_FAILED_MESSAGE: Final = (
    "The Test Runner could not list the phone's apps. Try again in a moment."
)
_RUN_STARTED_MESSAGE: Final = (
    "A run is using the phone. Pick apps from the phone when it has finished, "
    "or type the app ids instead."
)
_BUSY_MESSAGE: Final = (
    "The phone's apps are already being loaded from this Test Runner. Try "
    "again in a moment."
)
_NOT_YOURS_MESSAGE: Final = "Pick one of your Test Runners."
# What the picker reads of a runner (no other column is loaded).
_RUNNER_FIELDS: Final = (
    "name",
    "connected",
    "capabilities",
    "device_state",
    "device_reported_at",
)
# One load per runner at a time; outlives the round trip so a crashed request
# cannot block the button for longer than one timeout.
_LOAD_LOCK_SECONDS: Final = int(DEVICE_APPS_TIMEOUT_SECONDS) + 5
_UNSUPPORTED_MESSAGE: Final = (
    "This phone's driver cannot list the installed apps. Type the app ids instead."
)


@dataclass(frozen=True)
class PhoneAppRunnerChoice:
    """One of the viewer's phone runners the card may load the apps from."""

    runner_id: RunnerId
    # Owner-given name, escaped on render.
    name: str
    # It can be used now (runner new enough, device ready).
    enabled: bool
    # Why it cannot ("" when enabled); fixed text.
    reason: str


@dataclass(frozen=True)
class PhoneAppPicker:
    """Whether the card offers "Pick from the phone's apps", and from which of
    the viewer's Test Runners (A17: the person picks the runner)."""

    # At least one of the viewer's usable runners drives a phone: shown.
    show: bool
    # At least one of those runners can be used now.
    enabled: bool
    # Why none can ("" when enabled or not shown); fixed text.
    reason: str
    # The viewer's connected phone runners, by name.
    runners: tuple[PhoneAppRunnerChoice, ...]


@dataclass(frozen=True)
class PhoneAppRow:
    # Normalized id (``normalize_process_name``), the stored form.
    app_id: str
    # Runner text, escaped on render; "" when the platform reports none.
    name: str
    # Already in the project's allowlist.
    allowed: bool


@dataclass(frozen=True)
class PhoneAppList:
    rows: tuple[PhoneAppRow, ...]
    # The platform reports display names (iOS); Android shows ids only.
    names_available: bool
    # Apps the runner left out because their id is not a valid app id.
    skipped: int
    # The runner cut the list at its maximum.
    truncated: bool
    # How many more ids the allowlist takes (cap minus current count, >= 0).
    remaining: int


def phone_app_picker(user: CustomUser, project: Project) -> PhoneAppPicker:
    """The picker from the viewer's usable runners (two queries at most).

    Only connected runners that drive a phone or tablet are offered; a runner
    is never offered to anyone but its owner (``usable_runners``).
    """
    choices = tuple(
        _runner_choice(runner)
        for runner in usable_runners(user, project).only(*_RUNNER_FIELDS)
        if _drives_phone(runner)
    )
    if not choices:
        return PhoneAppPicker(show=False, enabled=False, reason="", runners=())
    enabled = any(choice.enabled for choice in choices)
    return PhoneAppPicker(
        show=True,
        enabled=enabled,
        reason="" if enabled else choices[0].reason,
        runners=choices,
    )


def load_phone_apps(
    project: Project, runner_id: RunnerId, *, actor: CustomUser
) -> PhoneAppList:
    """Ask the chosen phone runner for the device's installed apps.

    ``runner_id`` is the runner picked on the card; only its owner, a writable
    member of the project, may use it (``can_assign``). Holds no transaction
    during the round trip (up to ``DEVICE_APPS_TIMEOUT_SECONDS``). Refused
    while an agent run is Started on the runner (the listing would hold the
    runner's phone session and could fail the run's next action) and while
    another load from the same runner runs (per-runner cache lock). Raises
    ``CapturePhoneAppsError`` with fixed user text when the runner is not the
    actor's, not a connected phone runner, outdated, gone or silent, the
    reply is invalid, or the runner refused.
    """
    runner = (
        TestRunner.objects.filter(pk=runner_id, archived_at__isnull=True)
        .only("owner_id", "archived_at", *_RUNNER_FIELDS)
        .first()
    )
    if runner is None or not can_assign(actor, runner, project):
        raise CapturePhoneAppsError(_NOT_YOURS_MESSAGE)
    if not _drives_phone(runner):
        raise CapturePhoneAppsError(_NOT_CONNECTED_MESSAGE)
    choice = _runner_choice(runner)
    if not choice.enabled:
        raise CapturePhoneAppsError(choice.reason)
    if agent_run_started_on_runner(runner_id):
        raise CapturePhoneAppsError(_RUN_STARTED_MESSAGE)
    lock_key = f"capture_phone_apps:load:runner:{runner_id}"
    if not cache.add(lock_key, 1, timeout=_LOAD_LOCK_SECONDS):
        raise CapturePhoneAppsError(_BUSY_MESSAGE)
    try:
        result = _list_device_apps(runner_id)
    finally:
        cache.delete(lock_key)
    if result.status is DeviceAppsStatus.UNSUPPORTED:
        raise CapturePhoneAppsError(_UNSUPPORTED_MESSAGE)
    if result.status is DeviceAppsStatus.DEVICE_UNAVAILABLE:
        raise CapturePhoneAppsError(DEVICE_NOT_READY_MESSAGE)
    if result.status is not DeviceAppsStatus.OK:
        raise CapturePhoneAppsError(_FAILED_MESSAGE)
    allowed = frozenset(stored_capture_apps(project))
    return PhoneAppList(
        rows=tuple(
            PhoneAppRow(app_id=app.app_id, name=app.name, allowed=app.app_id in allowed)
            for app in result.apps
        ),
        names_available=result.names_available,
        skipped=result.skipped,
        truncated=result.truncated,
        remaining=max(CAPTURE_ALLOWED_PROCESSES_MAX - len(allowed), 0),
    )


def _drives_phone(runner: TestRunner) -> bool:
    """A connected runner that drives a phone or tablet (no query)."""
    return runner.connected and runner.device_state != RunnerDeviceState.NONE


def _runner_choice(runner: TestRunner) -> PhoneAppRunnerChoice:
    """Whether a connected phone runner can list its apps now (no query)."""
    reason = ""
    if not capabilities_include(runner.capabilities, ClientCapability.DEVICE_APPS_V1):
        reason = RUNNER_OUTDATED_MESSAGE
    elif device_offline(runner):
        reason = DEVICE_NOT_READY_MESSAGE
    return PhoneAppRunnerChoice(
        runner_id=runner.runner_id,
        name=runner.name,
        enabled=not reason,
        reason=reason,
    )


def _list_device_apps(runner_id: RunnerId) -> DeviceAppList:
    """The runner round trip with controller errors mapped to user text."""
    try:
        return controller_list_device_apps(runner_id)
    except ControllerNotConnectedError as exc:
        raise CapturePhoneAppsError(_NOT_CONNECTED_MESSAGE) from exc
    except DeviceDisconnectedError as exc:
        raise CapturePhoneAppsError(DEVICE_NOT_READY_MESSAGE) from exc
    except TestRunnerUnavailableError as exc:
        raise CapturePhoneAppsError(_UNRESPONSIVE_MESSAGE) from exc
    except ControllerActionError as exc:
        raise CapturePhoneAppsError(_FAILED_MESSAGE) from exc
