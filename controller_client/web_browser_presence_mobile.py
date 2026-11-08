"""Which phone browsers are running, for the web browser layer.

Android: one ``adb shell`` per check answers, for every browser package,
whether it has a process (``pidof``) and which DevTools sockets exist
(``/proc/net/unix``, the list chromedriver reads itself). iOS: the app state
of every browser bundle id, asked through the phone's Appium session in one
``web_call``. Both are cached briefly, so a list followed by an action
checks once; the hub caches on top of that.

adb is found as ``mobile_discovery`` finds it (PATH). Every adb call has a
short timeout; a failed call reads as "nothing running".
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from appium.webdriver.applicationstate import ApplicationState
from appium.webdriver.webdriver import WebDriver

from controller_client.exceptions import DeviceDisconnectedError, ExecutionError
from controller_client.mobile_device import MobileDevice

logger = logging.getLogger(__name__)

ADB_SHELL_TIMEOUT_SECONDS: Final[float] = 5.0
ADB_FORWARD_TIMEOUT_SECONDS: Final[float] = 2.0
_ADB_START_TIMEOUT_SECONDS: Final[float] = 10.0
_CACHE_SECONDS: Final[float] = 1.5
_SOCKET_MARKER: Final[str] = "devtools_remote"
_PID_PREFIX: Final[str] = "pid"
_VERSION_KEY: Final[str] = "versionName="
_VIEW_ACTION: Final[str] = "android.intent.action.VIEW"
_LAUNCHER_CATEGORY: Final[str] = "android.intent.category.LAUNCHER"
_NO_ADB: Final[str] = "adb is not available on the Test Runner host"
_START_FAILED: Final[str] = "The browser app could not be started on the phone"
# iOS app states that count as running (foreground or background).
IOS_RUNNING_STATES: Final[frozenset[int]] = frozenset(
    {
        ApplicationState.RUNNING_IN_BACKGROUND_SUSPENDED,
        ApplicationState.RUNNING_IN_BACKGROUND,
        ApplicationState.RUNNING_IN_FOREGROUND,
    }
)


def _adb_path() -> str | None:
    return shutil.which("adb")


def _adb(serial: str | None, args: list[str], timeout: float) -> str | None:
    """stdout of ``adb [-s serial] <args>`` (an argv list, no host shell),
    or None on failure; never raises."""
    adb = _adb_path()
    if adb is None:
        logger.debug(_NO_ADB)
        return None
    argv = [adb]
    if serial:
        argv += ["-s", serial]
    argv += args
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("adb %s failed: %s", args[0], exc)
        return None
    if result.returncode != 0 and not result.stdout:
        logger.debug("adb %s exited with %d", args[0], result.returncode)
        return None
    return result.stdout


def adb_shell(serial: str | None, command: str, timeout: float) -> str | None:
    """stdout of ``adb [-s serial] shell <command>``, or None on failure.

    ``command`` is one device shell line: every variable part must already
    be quoted with ``shlex.quote`` (adb joins the arguments into a string
    the device shell parses).
    """
    return _adb(serial, ["shell", command], timeout)


@dataclass(frozen=True)
class AdbForward:
    # ``adb forward --list`` columns: serial, local ("tcp:N"), remote.
    serial: str
    local: str
    remote: str


def adb_forwards(serial: str | None) -> frozenset[AdbForward] | None:
    """The host's adb port forwards (only ``serial``'s when given), or None
    when adb fails."""
    output = _adb(serial, ["forward", "--list"], ADB_FORWARD_TIMEOUT_SECONDS)
    if output is None:
        return None
    forwards: set[AdbForward] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        forward = AdbForward(parts[0], parts[1], parts[2])
        if serial and forward.serial != serial:
            continue
        forwards.add(forward)
    return frozenset(forwards)


def adb_remove_forward(serial: str | None, local: str) -> None:
    """``adb [-s serial] forward --remove <local>``; never raises."""
    if (
        _adb(serial, ["forward", "--remove", local], ADB_FORWARD_TIMEOUT_SECONDS)
        is None
    ):
        logger.debug("Removing the adb forward %s failed", local)


@dataclass(frozen=True)
class AndroidSnapshot:
    running: frozenset[str]
    # DevTools socket names without the abstract-namespace "@".
    sockets: frozenset[str]


class AndroidPresence:
    """The running browser packages and DevTools sockets of one device."""

    def __init__(self, serial: str | None, packages: tuple[str, ...]) -> None:
        self._serial = serial
        self._packages = packages
        self._lock = threading.Lock()
        self._snapshot = AndroidSnapshot(frozenset(), frozenset())
        self._at = 0.0

    def snapshot(self) -> AndroidSnapshot:
        with self._lock:
            if time.monotonic() - self._at < _CACHE_SECONDS:
                return self._snapshot
        snapshot = self._read()
        with self._lock:
            self._snapshot, self._at = snapshot, time.monotonic()
        return snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0

    def _read(self) -> AndroidSnapshot:
        names = " ".join(shlex.quote(package) for package in self._packages)
        # grep exits 1 with no match; the loop's output still arrives.
        command = (
            f'for p in {names}; do echo "{_PID_PREFIX} $p $(pidof $p)"; done; '
            f"grep -a {_SOCKET_MARKER} /proc/net/unix"
        )
        output = adb_shell(self._serial, command, ADB_SHELL_TIMEOUT_SECONDS)
        if output is None:
            return AndroidSnapshot(frozenset(), frozenset())
        return parse_android_snapshot(output)


def parse_android_snapshot(output: str) -> AndroidSnapshot:
    running: set[str] = set()
    sockets: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == _PID_PREFIX:
            if len(parts) >= 3:
                running.add(parts[1])
            continue
        name = parts[-1].lstrip("@")
        if _SOCKET_MARKER in name:
            sockets.add(name)
    return AndroidSnapshot(frozenset(running), frozenset(sockets))


def android_app_version(serial: str | None, package: str) -> str:
    """The app's ``versionName``, or "" when it is not installed or adb
    fails."""
    command = f"dumpsys package {shlex.quote(package)}"
    output = adb_shell(serial, command, ADB_SHELL_TIMEOUT_SECONDS)
    if output is None:
        return ""
    for line in output.splitlines():
        text = line.strip()
        if text.startswith(_VERSION_KEY):
            return text[len(_VERSION_KEY) :].strip()
    return ""


def android_start_app(serial: str | None, package: str, url: str | None) -> None:
    """Start ``package`` (with ``url`` through a VIEW intent aimed at that
    package when given). Raises ExecutionError when adb fails."""
    if _adb_path() is None:
        raise ExecutionError(_NO_ADB)
    if url is None:
        command = (
            f"monkey -p {shlex.quote(package)} -c {_LAUNCHER_CATEGORY} 1 "
            ">/dev/null 2>&1 && echo started"
        )
    else:
        command = (
            f"am start -a {_VIEW_ACTION} -d {shlex.quote(url)} "
            f"{shlex.quote(package)} >/dev/null 2>&1 && echo started"
        )
    output = adb_shell(serial, command, _ADB_START_TIMEOUT_SECONDS)
    if output is None or "started" not in output:
        raise ExecutionError(_START_FAILED)


class IosPresence:
    """The app states of the iOS browsers, asked in one phone call."""

    def __init__(self, mobile: MobileDevice, bundle_ids: tuple[str, ...]) -> None:
        self._mobile = mobile
        self._bundle_ids = bundle_ids
        self._lock = threading.Lock()
        self._states: Mapping[str, int] = {}
        self._at = 0.0

    def state(self, bundle_id: str) -> int:
        with self._lock:
            if time.monotonic() - self._at < _CACHE_SECONDS:
                return self._states.get(bundle_id, ApplicationState.NOT_RUNNING)
        states = self._mobile.web_call(self._query)
        with self._lock:
            self._states, self._at = states, time.monotonic()
        return states.get(bundle_id, ApplicationState.NOT_RUNNING)

    def is_running(self, bundle_id: str) -> bool:
        try:
            return self.state(bundle_id) in IOS_RUNNING_STATES
        except DeviceDisconnectedError:
            raise
        except ExecutionError as exc:
            # A busy phone reads as "not known": not open for this check.
            logger.debug("iOS app state check failed: %s", exc)
            return False

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0

    def _query(self, driver: WebDriver) -> dict[str, int]:
        states: dict[str, int] = {}
        for bundle_id in self._bundle_ids:
            value = driver.query_app_state(bundle_id)
            states[bundle_id] = value if isinstance(value, int) else 0
        return states
