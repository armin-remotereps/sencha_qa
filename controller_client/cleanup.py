from __future__ import annotations

import logging
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from controller_client.browser_executor import BrowserSession
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import ActionResultPayload

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClearDirectoryResult:
    removed: int
    skipped: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]

    @property
    def succeeded(self) -> bool:
        return not self.failed


def default_protected_paths() -> tuple[Path, ...]:
    """Paths the cleanup must never delete: this client's install dir and venv.

    The controller has been installed straight into ~/Downloads on client
    machines, so clearing that folder blindly deleted the running client's own
    site-packages until Windows refused to unlink a loaded .pyd.
    """
    return (Path(__file__).resolve().parent, Path(sys.prefix).resolve())


def execute_cleanup(
    browser_session: BrowserSession,
    session_manager: InteractiveSessionManager,
    process_tracker: ProcessTracker,
    cleanup_dir: Path,
) -> ActionResultPayload:
    start = time.monotonic()
    steps = [
        _close_browser(browser_session),
        _terminate_sessions(session_manager),
        _kill_processes(process_tracker),
    ]
    # Last, so processes that held files in the folder are already gone.
    clear_result = _clear_directory_safely(cleanup_dir)
    steps.append(_describe_clear(cleanup_dir, clear_result))
    duration_ms = (time.monotonic() - start) * 1000
    return ActionResultPayload(
        success=clear_result is None or clear_result.succeeded,
        message="; ".join(steps),
        duration_ms=duration_ms,
    )


def _close_browser(browser_session: BrowserSession) -> str:
    try:
        browser_session.close()
        return "browser closed"
    except Exception as exc:
        return f"browser close failed: {exc}"


def _terminate_sessions(session_manager: InteractiveSessionManager) -> str:
    try:
        session_manager.terminate_all()
        return "interactive sessions terminated"
    except Exception as exc:
        return f"session termination failed: {exc}"


def _kill_processes(process_tracker: ProcessTracker) -> str:
    try:
        killed = process_tracker.kill_all()
        return f"killed {len(killed)} tracked process(es)"
    except Exception as exc:
        return f"process kill failed: {exc}"


def _clear_directory_safely(directory: Path) -> ClearDirectoryResult | None:
    if not directory.exists():
        return None
    result = clear_directory(directory, default_protected_paths())
    if result.skipped:
        logger.warning(
            "Cleanup of %s skipped %d protected entr(y/ies) belonging to the "
            "controller itself: %s. Move the controller out of the cleanup "
            "folder or set CONTROLLER_CLEANUP_DIR to a different path.",
            directory,
            len(result.skipped),
            ", ".join(result.skipped),
        )
    for name, error in result.failed:
        logger.warning("Cleanup of %s could not remove %s: %s", directory, name, error)
    return result


def _describe_clear(directory: Path, result: ClearDirectoryResult | None) -> str:
    if result is None:
        return f"{directory} not found, skipped"
    parts = [f"{result.removed} removed"]
    if result.skipped:
        parts.append(
            f"{len(result.skipped)} protected skipped: {', '.join(result.skipped)}"
        )
    if result.failed:
        failures = ", ".join(f"{name} ({error})" for name, error in result.failed)
        parts.append(f"{len(result.failed)} failed: {failures}")
    return f"{directory} cleared ({'; '.join(parts)})"


def clear_directory(directory: Path, protected: Sequence[Path]) -> ClearDirectoryResult:
    removed = 0
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    for entry in directory.iterdir():
        if _is_protected(entry, protected):
            skipped.append(entry.name)
            continue
        try:
            _remove_entry(entry)
            removed += 1
        except Exception as exc:
            failed.append((entry.name, str(exc)))
    return ClearDirectoryResult(
        removed=removed, skipped=tuple(skipped), failed=tuple(failed)
    )


def _remove_entry(entry: Path) -> None:
    if entry.is_file() or entry.is_symlink():
        entry.unlink()
    else:
        shutil.rmtree(entry)


def _is_protected(entry: Path, protected: Sequence[Path]) -> bool:
    resolved = entry.resolve()
    return any(
        path == resolved
        or path.is_relative_to(resolved)
        or resolved.is_relative_to(path)
        for path in protected
    )
