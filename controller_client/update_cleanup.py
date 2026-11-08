"""The new runner's last update step: delete the old install.

The only code that deletes a runner folder. The path comes from the old
process (its own ``__file__``, passed on the command line), never from the
server, and ``validate_old_target`` must accept it. Blocking; run it in a
worker thread.
"""

import contextlib
import logging
import os
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

import psutil

from controller_client.exceptions import UpdateError
from controller_client.update_layout import (
    PACKAGE_DIR_NAME,
    WEB_PROFILES_DIR_NAME,
    move_web_profiles,
    validate_old_target,
)
from controller_client.web_browser_launch import find_profile_processes

logger = logging.getLogger(__name__)

OLD_PROCESS_EXIT_TIMEOUT_SECONDS: Final[float] = 120.0
_DELETE_ATTEMPTS: Final[int] = 5
_FIRST_RETRY_DELAY_SECONDS: Final[float] = 1.0
_CREATE_TIME_TOLERANCE: Final[float] = 0.01


def remove_old_install(
    target: Path, new_root: Path, replaces_pid: int | None, created: float | None
) -> bool:
    """Wait for the old runner to exit, then delete its install; True if deleted."""
    if replaces_pid is not None and not _wait_for_exit(replaces_pid, created):
        logger.error(
            "The old Test Runner (pid %d) is still running; not deleting %s",
            replaces_pid,
            target,
        )
        return False
    try:
        resolved = validate_old_target(target, new_root)
    except (UpdateError, OSError) as e:
        logger.error("Not deleting the old Test Runner folder %s: %s", target, e)
        return False
    # Keep the test browser profiles (saved logins) before the old folder
    # goes. A test browser still running from them would block the rename on
    # Windows and be left unreachable, so it is ended first.
    # A manual install's old target is its controller_client folder itself;
    # an update-made one is the test-runner-* folder around it.
    old_package = (
        resolved if resolved.name == PACKAGE_DIR_NAME else resolved / PACKAGE_DIR_NAME
    )
    old_profiles = old_package / WEB_PROFILES_DIR_NAME
    if old_profiles.is_dir():
        _end_processes_using(old_profiles)
    move_web_profiles(old_package, new_root / PACKAGE_DIR_NAME)
    return _delete_with_retries(resolved)


def _end_processes_using(profiles: Path) -> None:
    """End the test browsers started from the old install's profiles (the
    launcher's own profile match, ``find_profile_processes``); never raises."""
    found: list[psutil.Process] = []
    try:
        candidates = [profile for profile in profiles.iterdir() if profile.is_dir()]
    except OSError:
        logger.debug("Could not list the test browser profiles in %s", profiles)
        return
    for profile in candidates:
        try:
            found.extend(find_profile_processes(profile))
        except (OSError, psutil.Error):
            logger.debug("Could not list the processes of %s", profile)
    if not found:
        return
    logger.info("Closing %d test browser process(es) of the old install", len(found))
    for process in found:
        with contextlib.suppress(psutil.Error):
            process.terminate()
    _gone, alive = psutil.wait_procs(found, timeout=3)
    for process in alive:
        with contextlib.suppress(psutil.Error):
            process.kill()
    psutil.wait_procs(alive, timeout=1)


def _wait_for_exit(pid: int, created: float | None) -> bool:
    try:
        process = psutil.Process(pid)
        if (
            created is not None
            and abs(process.create_time() - created) > _CREATE_TIME_TOLERANCE
        ):
            # The PID was reused: the old runner is gone.
            return True
        process.wait(timeout=OLD_PROCESS_EXIT_TIMEOUT_SECONDS)
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False
    except psutil.Error as e:
        logger.warning("Could not watch the old Test Runner (pid %d): %s", pid, e)
        return False
    return True


def _clear_read_only(
    function: Callable[..., object], path: str, error: BaseException
) -> None:
    """``rmtree`` onexc hook: Windows refuses to delete read-only files."""
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _delete_with_retries(target: Path) -> bool:
    delay = _FIRST_RETRY_DELAY_SECONDS
    for attempt in range(1, _DELETE_ATTEMPTS + 1):
        try:
            _rmtree(target)
            logger.info("Deleted the old Test Runner folder %s", target)
            return True
        except FileNotFoundError:
            return not target.exists()
        except OSError as e:
            # Antivirus and the indexer hold short-lived locks on Windows.
            logger.warning(
                "Deleting %s failed (attempt %d/%d): %s",
                target,
                attempt,
                _DELETE_ATTEMPTS,
                e,
            )
            if attempt < _DELETE_ATTEMPTS:
                time.sleep(delay)
                delay *= 2
    return False


def _rmtree(target: Path) -> None:
    shutil.rmtree(target, onexc=_clear_read_only)
