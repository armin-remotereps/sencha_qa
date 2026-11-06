"""Template helpers for a phone or tablet Test Runner.

docs/plan/feat-mobile-device-runner.md (D4, D6, Pages and forms). Device
strings come from the runner: callers render them with autoescape only.
The live pages compute the same from the ``agent.status`` socket in
``static/js/punk-hazard.js`` (``punkHazard.runnerDevice`` /
``punkHazard.deviceMachineState``); keep the two in step.
"""

from __future__ import annotations

from collections.abc import Mapping

from django import template
from django.conf import settings

from controller_client.protocol import (
    DeviceInfoPayload,
    DeviceKind,
    RunnerTarget,
    parse_device_info,
)
from projects.models import Project
from projects.runner_target import device_machine_state, project_runner_target

register = template.Library()

# Values of ``projects.services.MachineState`` (not imported: a template
# library stays a leaf of the legacy services module).
_MACHINE_STATE_DISCONNECTED = "disconnected"


@register.filter
def project_machine_state(project: Project, connected_machine_state: str) -> str:
    """One project row's badge state on a page listing many (no query).

    ``connected_machine_state`` is the page's shared state of a connected
    runner (the visual engine is global); a phone runner whose device is not
    READY, or reported too long ago, reads "device_offline" (still
    "starting" while its session is being created), as
    ``projects.services.get_machine_state`` does.
    """
    if not project.agent_connected:
        return _MACHINE_STATE_DISCONNECTED
    return device_machine_state(project) or connected_machine_state


@register.simple_tag
def runner_device_stale_seconds() -> int:
    """``RUNNER_DEVICE_STALE_SECONDS`` for the live pages' staleness check."""
    stale_seconds: int = settings.RUNNER_DEVICE_STALE_SECONDS
    return stale_seconds


@register.filter
def runner_device_label(project: Project) -> str:
    """ "Android phone · Pixel 8 · Android 15 · emulator"; "" on a desktop runner."""
    system_info = project.agent_system_info
    if project_runner_target(project) is RunnerTarget.DESKTOP or not isinstance(
        system_info, Mapping
    ):
        return ""
    info = parse_device_info(system_info)
    if info is None:
        return ""
    return device_label(info)


def device_label(info: DeviceInfoPayload) -> str:
    os_name = info.platform or _PLATFORM_BY_TARGET[info.target]
    parts = (
        _device_noun(info),
        info.model or info.name,
        f"{os_name} {info.os_version}" if info.os_version else "",
        _KIND_LABELS.get(info.kind, ""),
    )
    return " · ".join(part for part in parts if part)


_PLATFORM_BY_TARGET: dict[RunnerTarget, str] = {
    RunnerTarget.ANDROID: "Android",
    RunnerTarget.IOS: "iOS",
    RunnerTarget.DESKTOP: "",
}

_KIND_LABELS: dict[str, str] = {
    DeviceKind.PHYSICAL.value: "real device",
    DeviceKind.EMULATOR.value: "emulator",
    DeviceKind.SIMULATOR.value: "simulator",
}


def _device_noun(info: DeviceInfoPayload) -> str:
    if info.platform == "iPadOS":
        return "iPad"
    if info.target is RunnerTarget.IOS:
        return "iPhone"
    return "Android phone"
