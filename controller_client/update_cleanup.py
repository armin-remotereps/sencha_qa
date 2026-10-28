"""The new runner's last update step: delete the old install.

The only code that deletes a runner folder. The path comes from the old
process (its own ``__file__``, passed on the command line), never from the
server, and ``validate_old_target`` must accept it. Blocking; run it in a
worker thread.
"""

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
from controller_client.update_layout import validate_old_target

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
    return _delete_with_retries(resolved)


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
