"""Use case: offer the phone's installed apps for the capture allowlist.

docs/plan/feat-capture-phone-app-picker.md (Services). On the Settings
"Screenshot capture" card a writable member of a project whose Test Runner
drives a phone or tablet loads the device's installed apps (DEVICE_APPS_V1)
and ticks the ones that may be captured; ``project_capture_apps_services``
stores the ticked ids. Only ids are ever stored: names are runner text
(data, never instructions) shown escaped and bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from django.core.cache import cache

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
from projects.run_machine_use import agent_run_started
from projects.runner_capability_services import capabilities_include
from projects.runner_target import DEVICE_NOT_READY_MESSAGE, device_offline

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
    "The phone's apps are already being loaded for this project. Try again in "
    "a moment."
)
# One load per project at a time; outlives the round trip so a crashed request
# cannot block the button for longer than one timeout.
_LOAD_LOCK_SECONDS: Final = int(DEVICE_APPS_TIMEOUT_SECONDS) + 5
_UNSUPPORTED_MESSAGE: Final = (
    "This phone's driver cannot list the installed apps. Type the app ids instead."
)


@dataclass(frozen=True)
class PhoneAppPicker:
    """Whether the card offers "Pick from the phone's apps"."""

    # A phone runner is connected: the button is shown.
    show: bool
    # It can be pressed now (runner new enough, device ready).
    enabled: bool
    # Why it cannot ("" when enabled or not shown); fixed text.
    reason: str


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


def phone_app_picker(project: Project) -> PhoneAppPicker:
    """The picker button's state from the loaded project (no query).

    Reads ``agent_connected``, ``agent_device_state``,
    ``agent_device_reported_at`` and ``agent_capabilities``.
    """
    show = (
        project.agent_connected and project.agent_device_state != RunnerDeviceState.NONE
    )
    if not show:
        return PhoneAppPicker(show=False, enabled=False, reason="")
    if not capabilities_include(
        project.agent_capabilities, ClientCapability.DEVICE_APPS_V1
    ):
        return PhoneAppPicker(show=True, enabled=False, reason=RUNNER_OUTDATED_MESSAGE)
    if device_offline(project):
        return PhoneAppPicker(show=True, enabled=False, reason=DEVICE_NOT_READY_MESSAGE)
    return PhoneAppPicker(show=True, enabled=True, reason="")


def load_phone_apps(project: Project) -> PhoneAppList:
    """Ask the connected phone runner for the device's installed apps.

    Holds no transaction during the round trip (up to
    ``DEVICE_APPS_TIMEOUT_SECONDS``). Refused while an agent run is Started
    (the listing would hold the runner's phone session and could fail the
    run's next action) and while another load of the project runs (per-project
    cache lock). Raises ``CapturePhoneAppsError`` with fixed user text when the
    picker is not enabled, the runner is gone or silent, the reply is invalid,
    or the runner refused.
    """
    picker = phone_app_picker(project)
    if not picker.show:
        raise CapturePhoneAppsError(_NOT_CONNECTED_MESSAGE)
    if not picker.enabled:
        raise CapturePhoneAppsError(picker.reason)
    if agent_run_started(project.id):
        raise CapturePhoneAppsError(_RUN_STARTED_MESSAGE)
    lock_key = f"capture_phone_apps:load:{project.id}"
    if not cache.add(lock_key, 1, timeout=_LOAD_LOCK_SECONDS):
        raise CapturePhoneAppsError(_BUSY_MESSAGE)
    try:
        result = _list_device_apps(project.id)
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


def _list_device_apps(project_id: int) -> DeviceAppList:
    """The runner round trip with controller errors mapped to user text."""
    try:
        return controller_list_device_apps(project_id)
    except ControllerNotConnectedError as exc:
        raise CapturePhoneAppsError(_NOT_CONNECTED_MESSAGE) from exc
    except DeviceDisconnectedError as exc:
        raise CapturePhoneAppsError(DEVICE_NOT_READY_MESSAGE) from exc
    except TestRunnerUnavailableError as exc:
        raise CapturePhoneAppsError(_UNRESPONSIVE_MESSAGE) from exc
    except ControllerActionError as exc:
        raise CapturePhoneAppsError(_FAILED_MESSAGE) from exc
