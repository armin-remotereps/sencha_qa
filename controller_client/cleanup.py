from __future__ import annotations

import shutil
import time
from pathlib import Path

from controller_client.browser_executor import BrowserSession
from controller_client.interactive_session import InteractiveSessionManager
from controller_client.process_tracker import ProcessTracker
from controller_client.protocol import ActionResultPayload


def execute_cleanup(
    browser_session: BrowserSession,
    session_manager: InteractiveSessionManager,
    process_tracker: ProcessTracker,
) -> ActionResultPayload:
    start = time.monotonic()
    steps = [
        _close_browser(browser_session),
        _terminate_sessions(session_manager),
        _kill_processes(process_tracker),
        _clear_downloads_folder(),
    ]
    duration_ms = (time.monotonic() - start) * 1000
    summary = "; ".join(steps)
    return ActionResultPayload(
        success=True,
        message=summary,
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


def _clear_downloads_folder() -> str:
    try:
        downloads_dir = Path.home() / "Downloads"
        if not downloads_dir.exists():
            return "Downloads folder not found, skipped"
        removed = _remove_all_entries(downloads_dir)
        return f"Downloads folder cleared ({removed} item(s) removed)"
    except Exception as exc:
        return f"Downloads folder clear failed: {exc}"


def _remove_all_entries(directory: Path) -> int:
    removed = 0
    for entry in directory.iterdir():
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
            else:
                shutil.rmtree(entry)
            removed += 1
        except Exception:
            pass
    return removed
