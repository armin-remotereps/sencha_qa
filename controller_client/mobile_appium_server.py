"""The Appium server a phone runner starts for itself on 127.0.0.1.

Started with ``CONTROLLER_APPIUM_COMMAND`` (``appium``) on a free local port,
never with ``--relaxed-security``, so ``adb shell`` and the like stay off.
Its output goes to a log file under the runner's log folder. Nothing is ever
installed: a missing Node.js, Appium or driver is reported with the commands
the person runs.
"""

from __future__ import annotations

import atexit
import json
import logging
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Final

import psutil

from controller_client.exceptions import ExecutionError
from controller_client.protocol import RunnerTarget

logger = logging.getLogger(__name__)

LOCAL_HOST: Final[str] = "127.0.0.1"
_START_TIMEOUT_SECONDS: Final[float] = 60.0
_POLL_SECONDS: Final[float] = 0.5
_STATUS_TIMEOUT_SECONDS: Final[float] = 3.0
_STOP_TIMEOUT_SECONDS: Final[float] = 10.0
_TOOL_TIMEOUT_SECONDS: Final[float] = 60.0
_LOG_TAIL_CHARS: Final[int] = 600
_MAX_STATUS_BYTES: Final[int] = 64 * 1024

# appium-xcuitest-driver 13 needs ^22.22.2 || ^24.15.0 || >=26; Appium 3 itself
# is satisfied by the same range.
_NODE_FLOORS: Final[dict[int, tuple[int, int]]] = {22: (22, 2), 24: (15, 0)}
_NODE_MIN_ANY: Final[int] = 26
NODE_REQUIREMENT: Final[str] = "Node.js 22.22.2+ or 24.15+"
_NODE_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")

APPIUM_INSTALL_HINT: Final[str] = (
    f"Install {NODE_REQUIREMENT} (https://nodejs.org), then run: " "npm i -g appium@3"
)
REQUIRED_DRIVER: Final[dict[RunnerTarget, str]] = {
    RunnerTarget.ANDROID: "uiautomator2",
    RunnerTarget.IOS: "xcuitest",
}


def driver_install_hint(driver: str) -> str:
    return f"Run: appium driver install {driver}"


def free_local_port() -> int:
    """A TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCAL_HOST, 0))
        port: int = sock.getsockname()[1]
        return port


def appium_argv(command: str) -> list[str]:
    """``command`` split with shlex and its program resolved on PATH.

    Raises ExecutionError naming the install commands when it is missing.
    """
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ExecutionError(f"CONTROLLER_APPIUM_COMMAND is not valid: {exc}") from exc
    if not argv:
        raise ExecutionError("CONTROLLER_APPIUM_COMMAND is empty")
    # On Windows npm installs appium.cmd; which() applies PATHEXT.
    program = shutil.which(argv[0])
    if program is None:
        raise ExecutionError(
            f"'{argv[0]}' (Appium) was not found on PATH. {APPIUM_INSTALL_HINT}"
        )
    return [program, *argv[1:]]


def node_version() -> tuple[int, int, int] | None:
    """The installed Node.js version, or None when ``node`` is missing."""
    node = shutil.which("node")
    if node is None:
        return None
    output = _run_tool([node, "--version"])
    match = _NODE_VERSION_RE.search(output or "")
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def node_version_supported(version: tuple[int, int, int]) -> bool:
    major, minor, patch = version
    if major >= _NODE_MIN_ANY:
        return True
    floor = _NODE_FLOORS.get(major)
    return floor is not None and (minor, patch) >= floor


def appium_version(command: str) -> str:
    """``appium --version``; raises ExecutionError when Appium cannot run."""
    output = _run_tool([*appium_argv(command), "--version"])
    if output is None:
        raise ExecutionError(f"'appium --version' failed. {APPIUM_INSTALL_HINT}")
    return output.strip().splitlines()[-1] if output.strip() else "unknown"


def installed_drivers(command: str) -> dict[str, str]:
    """Installed Appium drivers by name with their version.

    Raises ExecutionError when Appium cannot list them.
    """
    output = _run_tool(
        [*appium_argv(command), "driver", "list", "--installed", "--json"]
    )
    if output is None:
        raise ExecutionError(
            f"'appium driver list --installed --json' failed. {APPIUM_INSTALL_HINT}"
        )
    try:
        data = json.loads(output)
    except ValueError as exc:
        raise ExecutionError("Appium printed an unreadable driver list") from exc
    if not isinstance(data, Mapping):
        raise ExecutionError("Appium printed an unreadable driver list")
    drivers: dict[str, str] = {}
    for name, info in data.items():
        version = info.get("version") if isinstance(info, Mapping) else None
        drivers[str(name)] = version if isinstance(version, str) else ""
    return drivers


def check_prerequisites(command: str, target: RunnerTarget) -> None:
    """Raise ExecutionError naming the fix when Node, Appium or the
    target's driver is missing; installs nothing."""
    version = node_version()
    if version is None:
        raise ExecutionError(f"Node.js was not found on PATH. {APPIUM_INSTALL_HINT}")
    if not node_version_supported(version):
        found = ".".join(str(part) for part in version)
        raise ExecutionError(
            f"Node.js {found} is too old for Appium 3 and its drivers: install "
            f"{NODE_REQUIREMENT}."
        )
    driver = REQUIRED_DRIVER[target]
    if driver not in installed_drivers(command):
        raise ExecutionError(
            f"The Appium driver '{driver}' is not installed. "
            f"{driver_install_hint(driver)}"
        )


def appium_status(url: str) -> Mapping[str, object] | None:
    """The ``value`` of ``GET <url>/status``, or None when not answering."""
    request = urllib.request.Request(f"{url.rstrip('/')}/status", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_STATUS_TIMEOUT_SECONDS) as reply:
            body = reply.read(_MAX_STATUS_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    value = data.get("value") if isinstance(data, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _run_tool(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s failed: %s", argv[0], exc)
        return None
    if result.returncode != 0:
        logger.debug("%s exited %d: %s", argv, result.returncode, result.stderr[-300:])
        return None
    return result.stdout


class ManagedAppiumServer:
    """One Appium process bound to 127.0.0.1; ``start`` is idempotent."""

    def __init__(self, command: str, target: RunnerTarget, log_dir: Path) -> None:
        self._command = command
        self._target = target
        self._log_dir = log_dir
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_file: IO[bytes] | None = None
        self._log_path: Path | None = None
        self._url = ""
        self._atexit_registered = False

    @property
    def url(self) -> str:
        """The running server's URL, or "" when it is not running."""
        return self._url

    def start(self) -> str:
        """Start Appium (or reuse the running one) and return its URL.

        Raises ExecutionError with the reason and the install commands.
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if appium_status(self._url) is not None:
                    return self._url
            self._stop_locked()
            check_prerequisites(self._command, self._target)
            return self._spawn_locked()

    def stop(self) -> None:
        """Stop the server and its children; never raises."""
        with self._lock:
            self._stop_locked()

    def _spawn_locked(self) -> str:
        port = free_local_port()
        argv = [
            *appium_argv(self._command),
            "--address",
            LOCAL_HOST,
            "--port",
            str(port),
            "--log-no-colors",
            "--log-timestamp",
        ]
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._log_dir / f"appium-{port}.log"
        self._log_file = self._log_path.open("ab")
        logger.info(
            "Starting Appium on %s:%d (log %s)", LOCAL_HOST, port, self._log_path
        )
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self._close_log()
            raise ExecutionError(
                f"Appium could not be started ({exc}). {APPIUM_INSTALL_HINT}"
            ) from exc
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        self._url = f"http://{LOCAL_HOST}:{port}"
        self._wait_until_ready_locked()
        return self._url

    def _wait_until_ready_locked(self) -> None:
        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            process = self._process
            if process is None or process.poll() is not None:
                tail = self._log_tail()
                self._stop_locked()
                raise ExecutionError(
                    f"Appium exited while starting. {APPIUM_INSTALL_HINT}. "
                    f"Last log lines: {tail}"
                )
            if appium_status(self._url) is not None:
                return
            time.sleep(_POLL_SECONDS)
        tail = self._log_tail()
        self._stop_locked()
        raise ExecutionError(
            f"Appium did not answer within {_START_TIMEOUT_SECONDS:.0f} s. "
            f"Last log lines: {tail}"
        )

    def _log_tail(self) -> str:
        if self._log_path is None:
            return "(no log)"
        try:
            with self._log_path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - _LOG_TAIL_CHARS))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return "(log unreadable)"
        return " ".join(text.split()) or "(empty log)"

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._url = ""
        if process is not None:
            _terminate_tree(process)
        self._close_log()

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate Appium and its node children (appium.cmd on Windows is a
    cmd.exe wrapper); never raises."""
    try:
        root = psutil.Process(process.pid)
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    for child in children:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _, alive = psutil.wait_procs(children, timeout=_STOP_TIMEOUT_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Appium (pid %d) did not exit", process.pid)
