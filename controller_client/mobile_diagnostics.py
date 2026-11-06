"""Steps of ``python -m controller_client.diagnose --mobile``.

Prerequisites (Node.js, Appium, its driver, adb / Xcode), device discovery,
then a real session on the device: screenshot, points/pixels scale and, in
interactive mode after asking, a test tap. Each step reports what it saw.
"""

from __future__ import annotations

import atexit

from controller_client.diagnostics import DiagnosticStep
from controller_client.mobile_checks import device_steps, prerequisite_steps
from controller_client.mobile_device import MobileTargetBackend
from controller_client.mobile_discovery import (
    DiscoveredDevice,
    describe_missing_tools,
    discover_devices,
)
from controller_client.mobile_factory import build_mobile_backend
from controller_client.mobile_settings import MobileSettings
from controller_client.protocol import DeviceState


class _Probe:
    """The device found by discovery and the backend built for it."""

    def __init__(self, settings: MobileSettings) -> None:
        self._settings = settings
        self.device: DiscoveredDevice | None = None
        self._backend: MobileTargetBackend | None = None

    def discover(self) -> str:
        devices = discover_devices(self._settings.target)
        wanted = self._settings.device_id
        if wanted:
            matches = [device for device in devices if device.device_id == wanted]
            if not matches:
                raise RuntimeError(
                    f"CONTROLLER_DEVICE={wanted} is not connected" f"{_seen(devices)}"
                )
            device = matches[0]
        elif len(devices) == 1:
            device = devices[0]
        elif not devices:
            missing = describe_missing_tools(self._settings.target)
            raise RuntimeError(
                "no device, emulator or simulator is connected"
                + (f" ({missing})" if missing else "")
            )
        else:
            raise RuntimeError(
                f"{len(devices)} devices are connected{_seen(devices)}: set "
                "CONTROLLER_DEVICE to the one to check"
            )
        if device.state is not DeviceState.READY:
            raise RuntimeError(
                f"{device.name} ({device.device_id}) is {device.state.value}"
            )
        self.device = device
        return f"{device.name} ({device.device_id}), {device.kind.value}"

    def backend(self) -> MobileTargetBackend:
        if self._backend is None:
            if self.device is None:
                raise RuntimeError("no device was found")
            self._backend = build_mobile_backend(self._settings, self.device)
            # Quit the session and the managed Appium however diagnose ends.
            atexit.register(self._backend.stop)
        return self._backend


def _seen(devices: list[DiscoveredDevice]) -> str:
    if not devices:
        return ""
    return " (found: " + ", ".join(device.device_id for device in devices) + ")"


def mobile_diagnostic_steps(
    settings: MobileSettings, interactive: bool
) -> tuple[DiagnosticStep, ...]:
    probe = _Probe(settings)
    return (
        *prerequisite_steps(settings),
        DiagnosticStep("device", probe.discover),
        *device_steps(probe.backend, interactive),
    )
