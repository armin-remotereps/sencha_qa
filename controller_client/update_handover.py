"""Start the new runner detached from the old one, and watch the handover.

Windows: a new console window (its own process group, broken away from the
old runner's job when allowed); the elevation token is inherited. macOS and
Linux: its own session (no SIGHUP from the old terminal), stdin from
/dev/null and output to ``logs/runner-start.log``; the environment (DISPLAY,
WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS, XDG_*) is inherited.

The new runner writes a marker file once the server accepted its handshake,
so the old runner learns about the handover even if its own socket dropped
before the server could tell it it was superseded.
"""

import logging
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

from controller_client.exceptions import UpdateError
from controller_client.update_installer import subprocess_environment
from controller_client.update_layout import PACKAGE_DIR_NAME

logger = logging.getLogger(__name__)

LOGS_DIR_NAME: Final[str] = "logs"
START_LOG_NAME: Final[str] = "runner-start.log"
RUNNER_LOG_NAME: Final[str] = "runner.log"
_CREATE_TIME_TOLERANCE: Final[float] = 0.01
_TERMINATE_GRACE_SECONDS: Final[float] = 10.0
# ERROR_ACCESS_DENIED: the job the old runner is in forbids breakaway.
_WINERROR_ACCESS_DENIED: Final[int] = 5


@dataclass(frozen=True)
class SpawnedRunner:
    pid: int
    create_time: float
    # Where the new runner's output goes (the console window shows it too on
    # Windows).
    log_path: Path


def logs_dir(install_root: Path) -> Path:
    return install_root / PACKAGE_DIR_NAME / LOGS_DIR_NAME


def handover_marker(install_root: Path, update_id: str) -> Path:
    # update_id is a validated UUID, so it is safe in a file name.
    return logs_dir(install_root) / f"connected-{uuid.UUID(update_id)}"


def mark_connected(install_root: Path, update_id: str) -> None:
    """Called by the new runner after its first accepted handshake."""
    marker = handover_marker(install_root, update_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def is_handed_over(install_root: Path, update_id: str) -> bool:
    return handover_marker(install_root, update_id).is_file()


def spawn_new_runner(
    new_python: Path, new_root: Path, args: Sequence[str]
) -> SpawnedRunner:
    """Start ``<new_python> -m controller_client.main *args`` in ``new_root``."""
    command = [str(new_python), "-m", "controller_client.main", *args]
    log_dir = logs_dir(new_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = subprocess_environment()
    try:
        if sys.platform == "win32":
            process = _spawn_windows(command, new_root, env)
            log_path = log_dir / RUNNER_LOG_NAME
        else:
            log_path = log_dir / START_LOG_NAME
            with open(log_path, "ab") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=new_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
    except OSError as e:
        raise UpdateError(f"Could not start the new Test Runner: {e}") from e
    try:
        create_time = psutil.Process(process.pid).create_time()
    except psutil.Error as e:
        raise UpdateError(
            f"The new Test Runner exited right away; see {log_path}"
        ) from e
    logger.info("Started the new Test Runner (pid %d) in %s", process.pid, new_root)
    return SpawnedRunner(pid=process.pid, create_time=create_time, log_path=log_path)


if sys.platform == "win32":

    def _spawn_windows(
        command: list[str], cwd: Path, env: dict[str, str]
    ) -> "subprocess.Popen[bytes]":
        base_flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                creationflags=base_flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
            )
        except OSError as e:
            if getattr(e, "winerror", None) != _WINERROR_ACCESS_DENIED:
                raise
            logger.warning(
                "Cannot break away from the Test Runner's job; starting the new "
                "Test Runner inside it"
            )
            return subprocess.Popen(command, cwd=cwd, env=env, creationflags=base_flags)

else:

    def _spawn_windows(
        command: list[str], cwd: Path, env: dict[str, str]
    ) -> "subprocess.Popen[bytes]":
        raise UpdateError("Windows only")


def _process_of(spawned: SpawnedRunner) -> psutil.Process | None:
    """The spawned process if it still runs (a reused PID does not count)."""
    try:
        process = psutil.Process(spawned.pid)
        if abs(process.create_time() - spawned.create_time) > _CREATE_TIME_TOLERANCE:
            return None
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except psutil.Error:
        return None


def is_running(spawned: SpawnedRunner) -> bool:
    return _process_of(spawned) is not None


def terminate_runner(spawned: SpawnedRunner) -> None:
    """Stop the spawned runner and everything it started; best effort."""
    process = _process_of(spawned)
    if process is None:
        return
    try:
        processes = [*process.children(recursive=True), process]
    except psutil.Error:
        processes = [process]
    for item in processes:
        try:
            item.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(processes, timeout=_TERMINATE_GRACE_SECONDS)
    for item in alive:
        try:
            item.kill()
        except psutil.Error:
            pass
