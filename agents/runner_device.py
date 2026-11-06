"""The phone or tablet a Test Runner drives, as short prompt text.

A leaf module (no imports from ``agents.services``) shared by the tester,
planner, person-step and setup prompts. Device strings come from the runner,
which is not trusted: every value is kept to one short printable line, and
the device id itself never reaches a prompt (the agent reads it from
``$RUNNER_DEVICE_ID`` in the host shell).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from controller_client.protocol import (
    DeviceInfoPayload,
    DeviceKind,
    RunnerTarget,
    parse_device_info,
    parse_runner_target,
)

_VALUE_MAX_CHARS: Final = 40

_KIND_LABELS: Final[dict[str, str]] = {
    DeviceKind.PHYSICAL.value: "real device",
    DeviceKind.EMULATOR.value: "emulator",
    DeviceKind.SIMULATOR.value: "simulator",
}


@dataclass(frozen=True)
class RunnerDevice:
    """What the prompts say about a phone runner's device."""

    target: RunnerTarget
    # "Android phone", "Android tablet", "iPhone" or "iPad".
    noun: str
    # "an Android phone (Pixel 8, Android 15, emulator)".
    description: str
    # "macOS", "Windows" or "Linux": the Test Runner computer.
    host_os: str


def runner_target(system_info: Mapping[str, object] | None) -> RunnerTarget:
    return parse_runner_target(system_info or {})


def _clean(value: str) -> str:
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text[:_VALUE_MAX_CHARS]


def _is_tablet(info: DeviceInfoPayload) -> bool:
    words = f"{info.model} {info.name} {info.platform}".lower()
    return "ipad" in words or "tablet" in words


def _noun(info: DeviceInfoPayload) -> str:
    tablet = _is_tablet(info)
    if info.target == RunnerTarget.IOS:
        return "iPad" if tablet else "iPhone"
    return "Android tablet" if tablet else "Android phone"


def _platform_label(info: DeviceInfoPayload) -> str:
    if info.target == RunnerTarget.ANDROID:
        return "Android"
    return "iPadOS" if _is_tablet(info) else "iOS"


def host_os_label(system_info: Mapping[str, object] | None) -> str:
    os_name = (system_info or {}).get("os")
    if os_name == "Darwin":
        return "macOS"
    if os_name == "Windows":
        return "Windows"
    return "Linux"


def runner_device(system_info: Mapping[str, object] | None) -> RunnerDevice | None:
    """The device of a phone runner, or None for a desktop runner."""
    info = parse_device_info(system_info or {})
    if info is None:
        return None
    noun = _noun(info)
    os_version = _clean(info.os_version)
    details = [
        part
        for part in (
            _clean(info.model) or _clean(info.name),
            f"{_platform_label(info)} {os_version}" if os_version else "",
            _KIND_LABELS.get(info.kind, ""),
        )
        if part
    ]
    article = "an" if noun[0] in "AEIOUaeiou" else "a"
    description = f"{article} {noun}" + (f" ({', '.join(details)})" if details else "")
    return RunnerDevice(
        target=info.target,
        noun=noun,
        description=description,
        host_os=host_os_label(system_info),
    )
