"""``list_device_apps``: the installed apps of the phone or tablet.

Reads the app list from the phone's driver (``MobileDevice.installed_apps``)
and returns it in the allowlist form: every id normalized with
``normalize_process_name`` (ids that do not normalize are skipped and
counted), duplicates merged, sorted by name then id and capped at
DEVICE_APPS_MAX. A refusal is a status, never an exception. Only counts are
logged, never app names or ids.
"""

from __future__ import annotations

import logging
from typing import Final

from controller_client.exceptions import DeviceDisconnectedError, ExecutionError
from controller_client.protocol import (
    DEVICE_APP_NAME_MAX_CHARS,
    DEVICE_APPS_MAX,
    DeviceAppPayload,
    DeviceAppsResultPayload,
    DeviceAppsStatus,
    RunnerTarget,
    normalize_process_name,
)
from controller_client.target import current_target, get_mobile_device

logger = logging.getLogger(__name__)

_UNAVAILABLE_DETAIL: Final[str] = (
    "The phone is not ready: it is disconnected or its automation session is "
    "still starting."
)
_UNSUPPORTED_DETAIL: Final[str] = "The phone's driver cannot list the installed apps."
_FAILED_DETAIL: Final[str] = "Reading the installed apps from the phone failed."


def list_device_apps() -> DeviceAppsResultPayload:
    """The installed apps of the phone (blocking; one Appium call)."""
    device = get_mobile_device()
    if device is None:
        return refusal(DeviceAppsStatus.DEVICE_UNAVAILABLE, _UNAVAILABLE_DETAIL)
    try:
        raw = device.installed_apps()
    except DeviceDisconnectedError:
        logger.info("Device apps: the phone is not ready")
        return refusal(DeviceAppsStatus.DEVICE_UNAVAILABLE, _UNAVAILABLE_DETAIL)
    except ExecutionError:
        logger.warning("Device apps: listing failed", exc_info=True)
        return refusal(DeviceAppsStatus.FAILED, _FAILED_DETAIL)
    if raw is None:
        return refusal(DeviceAppsStatus.UNSUPPORTED, _UNSUPPORTED_DETAIL)
    names: dict[str, str] = {}
    skipped = 0
    for raw_id, raw_name in raw.items():
        app_id = normalize_process_name(raw_id)
        if app_id is None:
            skipped += 1
            continue
        name = raw_name.strip()[:DEVICE_APP_NAME_MAX_CHARS]
        if not names.get(app_id):
            names[app_id] = name
    apps = sorted(
        (DeviceAppPayload(app_id=app_id, name=name) for app_id, name in names.items()),
        key=lambda app: (app.name.casefold() or app.app_id, app.app_id),
    )
    truncated = len(apps) > DEVICE_APPS_MAX
    logger.info(
        "Device apps: %d listed, %d unique, %d skipped%s",
        len(raw),
        len(apps),
        skipped,
        ", cut to the maximum" if truncated else "",
    )
    return DeviceAppsResultPayload(
        status=DeviceAppsStatus.OK,
        detail="",
        apps=tuple(apps[:DEVICE_APPS_MAX]),
        names_available=current_target() is RunnerTarget.IOS,
        skipped=skipped,
        truncated=truncated,
    )


def refusal(status: DeviceAppsStatus, detail: str) -> DeviceAppsResultPayload:
    """A result without apps (``status`` other than OK)."""
    return DeviceAppsResultPayload(
        status=status,
        detail=detail,
        apps=(),
        names_available=False,
        skipped=0,
        truncated=False,
    )
