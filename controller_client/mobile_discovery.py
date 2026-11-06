"""List phones, tablets, emulators and simulators without Appium.

Android: ``adb devices -l`` (plus one ``getprop`` per ready device for the
model and version when listing for a person). iOS: ``xcrun devicectl list
devices`` for real devices and ``xcrun simctl list devices booted`` for
simulators (macOS with Xcode only). Every call is bounded, takes a list argv
and never raises: a missing tool reads as no devices, and
``describe_missing_tools`` says what is missing.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from controller_client.protocol import DeviceKind, DeviceState, RunnerTarget

logger = logging.getLogger(__name__)

_ADB_TIMEOUT_SECONDS: Final[float] = 10.0
_GETPROP_TIMEOUT_SECONDS: Final[float] = 5.0
_XCRUN_TIMEOUT_SECONDS: Final[float] = 20.0
# devicectl writes its JSON to a file; a reply larger than this is not read.
_DEVICECTL_MAX_JSON_BYTES: Final[int] = 4 * 1024 * 1024

_ADB_LIST_HEADER: Final[str] = "List of devices"
_ADB_STATE_READY: Final[str] = "device"
_ADB_STATE_UNAUTHORIZED: Final[str] = "unauthorized"
_ADB_EMULATOR_PREFIX: Final[str] = "emulator-"
_ADB_PROP_SEPARATOR: Final[str] = "|"
# One device shell line (not a host shell): model and version in one call.
_ADB_GETPROP_SCRIPT: Final[str] = (
    "echo $(getprop ro.product.model)"
    f"'{_ADB_PROP_SEPARATOR}'$(getprop ro.build.version.release)"
)

_IOS_DEVICE_TYPES: Final[frozenset[str]] = frozenset({"iphone", "ipad", "ipod"})
_SIM_RUNTIME_RE: Final[re.Pattern[str]] = re.compile(
    r"SimRuntime\.iOS-(\d+)-(\d+)(?:-(\d+))?$"
)

_MISSING_ADB: Final[str] = (
    "adb (Android platform-tools) is not on PATH. Install Android "
    "platform-tools (Android Studio > SDK Manager, or 'brew install "
    "android-platform-tools' / 'winget install Google.PlatformTools') and add "
    "its folder to PATH, then restart the Test Runner."
)
_IOS_NEEDS_MAC: Final[str] = (
    "iPhones, iPads and iOS simulators can only be driven from a Mac with "
    "Xcode. Run the Test Runner on a Mac, or use an Android device."
)
_MISSING_XCODE: Final[str] = (
    "Xcode is not installed or not selected (the Command Line Tools alone "
    "have no simctl/devicectl). Install Xcode from the App Store, open it once, "
    "then run: sudo xcode-select -s /Applications/Xcode.app"
)


@dataclass(frozen=True)
class DiscoveredDevice:
    # Serial (Android) or UDID (iOS); what Appium's ``udid`` takes.
    device_id: str
    target: RunnerTarget
    kind: DeviceKind
    name: str
    model: str
    os_version: str
    # READY: present and usable; UNAUTHORIZED / DISCONNECTED otherwise.
    state: DeviceState


def discover_devices(target: RunnerTarget) -> list[DiscoveredDevice]:
    """Every device of ``target`` this host sees; never raises."""
    try:
        if target is RunnerTarget.ANDROID:
            return _android_devices(with_details=True)
        if target is RunnerTarget.IOS:
            return _ios_real_devices() + _ios_simulators()
    except Exception:
        logger.exception("Listing %s devices failed", target.value)
    return []


def find_device(
    target: RunnerTarget, device_id: str, kind: DeviceKind | None = None
) -> DiscoveredDevice | None:
    """The device with ``device_id``, or None; never raises.

    Cheaper than ``discover_devices`` (no per-device ``getprop``), so the
    device monitor can call it every few seconds. A known iOS ``kind`` runs
    only its lister (simctl for a simulator, devicectl for a real device).
    """
    try:
        if target is RunnerTarget.ANDROID:
            candidates = _android_devices(with_details=False)
        elif target is RunnerTarget.IOS:
            candidates = _ios_candidates(kind)
        else:
            return None
    except Exception:
        logger.exception("Looking up %s device %s failed", target.value, device_id)
        return None
    for device in candidates:
        if device.device_id == device_id:
            return device
    return None


def _ios_candidates(kind: DeviceKind | None) -> list[DiscoveredDevice]:
    if kind is DeviceKind.SIMULATOR:
        return _ios_simulators()
    if kind is DeviceKind.PHYSICAL:
        return _ios_real_devices()
    return _ios_simulators() + _ios_real_devices()


def describe_missing_tools(target: RunnerTarget) -> str | None:
    """What this host lacks to see devices of ``target``, or None."""
    if target is RunnerTarget.ANDROID:
        return None if _adb_path() is not None else _MISSING_ADB
    if target is RunnerTarget.IOS:
        if sys.platform != "darwin":
            return _IOS_NEEDS_MAC
        if not _xcrun_finds("simctl") or not _xcrun_finds("devicectl"):
            return _MISSING_XCODE
        return None
    return None


def _run(argv: list[str], timeout: float) -> str | None:
    """stdout of a successful run, else None (logged)."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s failed: %s", argv[0], exc)
        return None
    if result.returncode != 0:
        logger.debug(
            "%s exited with %d: %s", argv[:3], result.returncode, result.stderr[-300:]
        )
        return None
    return result.stdout


def _adb_path() -> str | None:
    return shutil.which("adb")


def _android_devices(*, with_details: bool) -> list[DiscoveredDevice]:
    adb = _adb_path()
    if adb is None:
        return []
    output = _run([adb, "devices", "-l"], _ADB_TIMEOUT_SECONDS)
    if output is None:
        return []
    devices: list[DiscoveredDevice] = []
    for line in output.splitlines():
        device = _parse_adb_line(line)
        if device is None:
            continue
        if with_details and device.state is DeviceState.READY:
            device = _with_android_props(adb, device)
        devices.append(device)
    return devices


def _parse_adb_line(line: str) -> DiscoveredDevice | None:
    # "<serial> <state> [usb:...] [product:...] [model:Pixel_8] [device:...]"
    parts = line.split()
    # Skips the "List of devices attached" header and "* daemon ..." notes.
    if len(parts) < 2 or line.startswith(("*", _ADB_LIST_HEADER)):
        return None
    serial, raw_state = parts[0], parts[1]
    fields = dict(part.split(":", 1) for part in parts[2:] if ":" in part)
    model = fields.get("model", "").replace("_", " ")
    return DiscoveredDevice(
        device_id=serial,
        target=RunnerTarget.ANDROID,
        kind=(
            DeviceKind.EMULATOR
            if serial.startswith(_ADB_EMULATOR_PREFIX)
            else DeviceKind.PHYSICAL
        ),
        name=model or serial,
        model=model,
        os_version="",
        state=_adb_state(raw_state),
    )


def _adb_state(raw: str) -> DeviceState:
    if raw == _ADB_STATE_READY:
        return DeviceState.READY
    if raw == _ADB_STATE_UNAUTHORIZED:
        return DeviceState.UNAUTHORIZED
    # offline, "no permissions", bootloader, recovery, ...
    return DeviceState.DISCONNECTED


def _with_android_props(adb: str, device: DiscoveredDevice) -> DiscoveredDevice:
    output = _run(
        [adb, "-s", device.device_id, "shell", _ADB_GETPROP_SCRIPT],
        _GETPROP_TIMEOUT_SECONDS,
    )
    if output is None or _ADB_PROP_SEPARATOR not in output:
        return device
    model, version = (part.strip() for part in output.split(_ADB_PROP_SEPARATOR, 1))
    return DiscoveredDevice(
        device_id=device.device_id,
        target=device.target,
        kind=device.kind,
        name=model or device.name,
        model=model or device.model,
        os_version=version,
        state=device.state,
    )


def _xcrun_finds(tool: str) -> bool:
    if shutil.which("xcrun") is None:
        return False
    return _run(["xcrun", "--find", tool], _XCRUN_TIMEOUT_SECONDS) is not None


def _ios_simulators() -> list[DiscoveredDevice]:
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        return []
    output = _run(
        ["xcrun", "simctl", "list", "devices", "booted", "-j"], _XCRUN_TIMEOUT_SECONDS
    )
    if output is None:
        return []
    try:
        data = json.loads(output)
    except ValueError:
        return []
    runtimes = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(runtimes, dict):
        return []
    devices: list[DiscoveredDevice] = []
    for runtime, entries in runtimes.items():
        version = _simulator_os_version(str(runtime))
        if version is None or not isinstance(entries, list):
            continue
        for entry in entries:
            device = _parse_simulator(entry, version)
            if device is not None:
                devices.append(device)
    return devices


def _simulator_os_version(runtime: str) -> str | None:
    # "com.apple.CoreSimulator.SimRuntime.iOS-17-4" -> "17.4"; None for
    # watchOS, tvOS and visionOS runtimes.
    match = _SIM_RUNTIME_RE.search(runtime)
    if match is None:
        return None
    return ".".join(part for part in match.groups() if part)


def _parse_simulator(entry: object, version: str) -> DiscoveredDevice | None:
    if not isinstance(entry, Mapping):
        return None
    udid = entry.get("udid")
    name = entry.get("name")
    if not isinstance(udid, str) or not isinstance(name, str):
        return None
    if entry.get("state") != "Booted" or entry.get("isAvailable") is False:
        return None
    return DiscoveredDevice(
        device_id=udid,
        target=RunnerTarget.IOS,
        kind=DeviceKind.SIMULATOR,
        name=name,
        model=name,
        os_version=version,
        state=DeviceState.READY,
    )


def _ios_real_devices() -> list[DiscoveredDevice]:
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        return []
    with tempfile.TemporaryDirectory(prefix="ph-devicectl-") as folder:
        json_path = Path(folder) / "devices.json"
        output = _run(
            [
                "xcrun",
                "devicectl",
                "list",
                "devices",
                "--quiet",
                "--json-output",
                str(json_path),
            ],
            _XCRUN_TIMEOUT_SECONDS,
        )
        if output is None or not json_path.is_file():
            return []
        if json_path.stat().st_size > _DEVICECTL_MAX_JSON_BYTES:
            return []
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    result = data.get("result") if isinstance(data, dict) else None
    entries = result.get("devices") if isinstance(result, dict) else None
    if not isinstance(entries, list):
        return []
    devices: list[DiscoveredDevice] = []
    for entry in entries:
        device = _parse_devicectl_entry(entry)
        if device is not None:
            devices.append(device)
    return devices


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _parse_devicectl_entry(entry: object) -> DiscoveredDevice | None:
    data = _mapping(entry)
    hardware = _mapping(data.get("hardwareProperties"))
    properties = _mapping(data.get("deviceProperties"))
    connection = _mapping(data.get("connectionProperties"))
    # Appium wants the hardware UDID, not CoreDevice's own identifier.
    udid = _text(hardware, "udid")
    if not udid or _text(hardware, "deviceType").lower() not in _IOS_DEVICE_TYPES:
        return None
    model = _text(hardware, "marketingName") or _text(hardware, "productType")
    return DiscoveredDevice(
        device_id=udid,
        target=RunnerTarget.IOS,
        kind=DeviceKind.PHYSICAL,
        name=_text(properties, "name") or model or udid,
        model=model,
        os_version=_text(properties, "osVersionNumber"),
        state=_devicectl_state(connection, properties),
    )


def _devicectl_state(
    connection: Mapping[str, object], properties: Mapping[str, object]
) -> DeviceState:
    # tunnelState "unavailable": paired before but not reachable now;
    # "disconnected" only means no tunnel is up yet (it comes up on use).
    if _text(connection, "tunnelState") == "unavailable":
        return DeviceState.DISCONNECTED
    if _text(connection, "pairingState") not in ("", "paired"):
        return DeviceState.UNAUTHORIZED
    if _text(properties, "developerModeStatus") == "disabled":
        return DeviceState.UNAUTHORIZED
    return DeviceState.READY
