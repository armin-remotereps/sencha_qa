"""close_app / close_tab on macOS.

Apps are found from the window server (``CGWindowListCopyWindowInfo``) and
re-read per pid with ``NSRunningApplication``. ``NSWorkspace``'s app list and
``frontmostApplication`` only refresh while the main run loop runs, which it
never does in this asyncio process, so on their own they would go stale.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import psutil

from controller_client.app_close_policy import (
    FORCE_CLOSE_TIMEOUT_SECONDS,
    GRACEFUL_CLOSE_TIMEOUT_SECONDS,
    RUNNER_TAB_REFUSAL,
    UNKNOWN_TAB_REFUSAL,
    RunnerProcesses,
    best_matches,
    closed_message,
    closed_tab_message,
    force_failed_message,
    front_app_mismatch_message,
    hosts_runner_message,
    match_score,
    no_match_message,
    refused_message,
    still_open_message,
    wait_until,
)
from controller_client.app_discovery import MATCH_THRESHOLD
from controller_client.executor import execute_key_press
from controller_client.protocol import KeyPressPayload

if sys.platform == "darwin":
    import AppKit
    import Quartz

    _OS_SHELL_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
        {
            "com.apple.finder",
            "com.apple.dock",
            "com.apple.loginwindow",
            "com.apple.systemuiserver",
            "com.apple.WindowManager",
            "com.apple.controlcenter",
        }
    )
    # AppleScript that prints the tty of the tab cmd+w would close.
    _FRONT_TAB_TTY_SCRIPTS: Final[dict[str, str]] = {
        "com.apple.Terminal": (
            'tell application id "com.apple.Terminal" to get tty of selected tab '
            "of front window"
        ),
        "com.googlecode.iterm2": (
            'tell application id "com.googlecode.iterm2" to get tty of current '
            "session of current window"
        ),
    }
    # Terminal apps that may host the runner without being its ancestor (iTerm2
    # 3.4+ runs sessions under iTermServer, a child of launchd). When the runner
    # has a controlling tty, close_app never quits them.
    _TERMINAL_BUNDLE_IDS: Final[frozenset[str]] = frozenset(
        {
            *_FRONT_TAB_TTY_SCRIPTS,
            "dev.warp.Warp-Stable",
            "dev.warp.Warp",
            "org.alacritty",
            "net.kovidgoyal.kitty",
            "com.github.wez.wezterm",
            "com.mitchellh.ghostty",
            "co.zeit.hyper",
            "org.tabby",
        }
    )
    # A first-use Automation prompt blocks osascript; give up and refuse.
    _OSASCRIPT_TIMEOUT_SECONDS: Final[float] = 5.0
    _NORMAL_WINDOW_LAYER: Final[int] = 0

    @dataclass(frozen=True)
    class _MacApp:
        name: str
        bundle_path: str
        bundle_id: str
        pid: int
        handle: AppKit.NSRunningApplication

    def _app_for_pid(pid: int) -> _MacApp | None:
        """A regular (Dock) app for ``pid``, or None."""
        handle = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(
            pid
        )
        if handle is None or handle.isTerminated():
            return None
        if handle.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular:
            return None
        url = handle.bundleURL()
        bundle_path = (url.path() if url is not None else None) or ""
        name = handle.localizedName() or Path(bundle_path).stem or f"pid {pid}"
        return _MacApp(
            name=name,
            bundle_path=bundle_path,
            bundle_id=handle.bundleIdentifier() or "",
            pid=pid,
            handle=handle,
        )

    def _window_owner_pids(options: int) -> list[int]:
        """Owners of normal-layer windows, front to back, without repeats."""
        windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        pids: list[int] = []
        for window in windows or ():
            layer = window.get(Quartz.kCGWindowLayer)
            pid = window.get(Quartz.kCGWindowOwnerPID)
            if layer != _NORMAL_WINDOW_LAYER or not isinstance(pid, int):
                continue
            if pid not in pids:
                pids.append(pid)
        return pids

    def _running_apps() -> list[_MacApp]:
        # Window owners are always current; NSWorkspace adds apps that have no
        # window right now (it may miss apps launched since the runner started,
        # which the window list covers).
        pids = _window_owner_pids(
            Quartz.kCGWindowListOptionAll | Quartz.kCGWindowListExcludeDesktopElements
        )
        for handle in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
            pid = int(handle.processIdentifier())
            if pid not in pids:
                pids.append(pid)
        apps: list[_MacApp] = []
        for pid in pids:
            app = _app_for_pid(pid)
            if app is not None:
                apps.append(app)
        return apps

    def _frontmost_app() -> _MacApp | None:
        for pid in _window_owner_pids(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        ):
            app = _app_for_pid(pid)
            if app is not None:
                return app
        return None

    def _score(query: str, app: _MacApp) -> float:
        return match_score(query, app.name, app.bundle_path)

    def _may_host_runner(app: _MacApp, runner: RunnerProcesses) -> bool:
        if runner.hosts_runner(app.pid):
            return True
        return runner.terminal is not None and app.bundle_id in _TERMINAL_BUNDLE_IDS

    def _is_protected(app: _MacApp, runner: RunnerProcesses) -> bool:
        return (
            runner.is_runner(app.pid)
            or _may_host_runner(app, runner)
            or app.bundle_id in _OS_SHELL_BUNDLE_IDS
        )

    def _refusal(protected: list[_MacApp], runner: RunnerProcesses) -> str:
        for app in protected:
            if _may_host_runner(app, runner):
                return hosts_runner_message(app.name)
        return refused_message(protected[0].name)

    def _all_exited(apps: list[_MacApp]) -> bool:
        return not any(psutil.pid_exists(app.pid) for app in apps)

    def close_app(query: str, force: bool, runner: RunnerProcesses) -> tuple[bool, str]:
        apps = _running_apps()
        matches = best_matches(query, apps, _score)
        if not matches:
            ranked = [(app.name, _score(query, app)) for app in apps]
            return False, no_match_message(query, ranked)
        targets = [app for app in matches if not _is_protected(app, runner)]
        protected = [app for app in matches if _is_protected(app, runner)]
        if not targets:
            return False, _refusal(protected, runner)

        for app in targets:
            if force:
                app.handle.forceTerminate()
            else:
                app.handle.terminate()
        timeout = (
            FORCE_CLOSE_TIMEOUT_SECONDS if force else GRACEFUL_CLOSE_TIMEOUT_SECONDS
        )
        if not wait_until(lambda: _all_exited(targets), timeout):
            still_open = [app.name for app in targets if psutil.pid_exists(app.pid)]
            if force:
                return False, force_failed_message(still_open)
            return False, still_open_message(still_open)
        return True, closed_message(
            [app.name for app in targets], [app.name for app in protected]
        )

    def _front_tab_tty(script: str) -> str | None:
        try:
            completed = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=_OSASCRIPT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        tty = completed.stdout.strip()
        if completed.returncode != 0 or not tty.startswith("/dev/"):
            return None
        return tty

    def _tab_refusal(app: _MacApp, runner: RunnerProcesses) -> str | None:
        """Why the front tab of ``app`` must stay open, or None when it may close."""
        if runner.is_runner(app.pid):
            return RUNNER_TAB_REFUSAL
        script = _FRONT_TAB_TTY_SCRIPTS.get(app.bundle_id)
        if script is not None:
            # Compared whatever the ancestry says: the terminal may host the
            # runner through a server process that is not its ancestor.
            tty = _front_tab_tty(script)
            if tty is None:
                return UNKNOWN_TAB_REFUSAL
            if tty == runner.terminal:
                return RUNNER_TAB_REFUSAL
            if runner.terminal is None and runner.hosts_runner(app.pid):
                return UNKNOWN_TAB_REFUSAL
            return None
        if app.bundle_id in _TERMINAL_BUNDLE_IDS or runner.hosts_runner(app.pid):
            # No way to read which tab is in front.
            return UNKNOWN_TAB_REFUSAL
        return None

    def close_tab(query: str, runner: RunnerProcesses) -> tuple[bool, str]:
        front = _frontmost_app()
        if front is None:
            return False, front_app_mismatch_message("No app", query)
        if _score(query, front) < MATCH_THRESHOLD:
            return False, front_app_mismatch_message(front.name, query)
        refusal = _tab_refusal(front, runner)
        if refusal is not None:
            return False, refusal
        execute_key_press(KeyPressPayload(keys="cmd+w"))
        return True, closed_tab_message(front.name)
