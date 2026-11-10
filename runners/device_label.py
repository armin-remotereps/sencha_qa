"""The phone or tablet a phone Test Runner drives, as one label (a leaf).

"Android phone · Pixel 8 · Android 15 · emulator"; "" for a desktop runner.
Used by the runner list and detail services and the ``runner_device_label``
template filter. The live pages compute the same from the ``runner.status``
socket in ``static/js/punk-hazard.js`` (``punkHazard.runnerDevice``); keep
the two in step. Device strings come from the runner: render them escaped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from controller_client.protocol import (
    DeviceInfoPayload,
    DeviceKind,
    RunnerTarget,
    parse_device_info,
)
from projects.runner_target import runner_target_of
from runners.models import TestRunner

_PLATFORM_BY_TARGET: Final[dict[RunnerTarget, str]] = {
    RunnerTarget.ANDROID: "Android",
    RunnerTarget.IOS: "iOS",
    RunnerTarget.DESKTOP: "",
}

_KIND_LABELS: Final[dict[str, str]] = {
    DeviceKind.PHYSICAL.value: "real device",
    DeviceKind.EMULATOR.value: "emulator",
    DeviceKind.SIMULATOR.value: "simulator",
}


def runner_device_label(runner: TestRunner | None) -> str:
    """The runner's device label from its loaded ``system_info`` (no query);
    "" on a desktop runner or without one."""
    if runner is None:
        return ""
    system_info = runner.system_info
    if runner_target_of(runner) is RunnerTarget.DESKTOP or not isinstance(
        system_info, Mapping
    ):
        return ""
    info = parse_device_info(system_info)
    return "" if info is None else device_label(info)


def device_label(info: DeviceInfoPayload) -> str:
    os_name = info.platform or _PLATFORM_BY_TARGET[info.target]
    parts = (
        _device_noun(info),
        info.model or info.name,
        f"{os_name} {info.os_version}" if info.os_version else "",
        _KIND_LABELS.get(info.kind, ""),
    )
    return " · ".join(part for part in parts if part)


def _device_noun(info: DeviceInfoPayload) -> str:
    if info.platform == "iPadOS":
        return "iPad"
    if info.target is RunnerTarget.IOS:
        return "iPhone"
    return "Android phone"
