"""close_app / close_tab on Windows.

Apps are the visible, titled, unowned top-level windows (``EnumWindows``). A
window is matched on the app-name part of its title ("Untitled - Notepad" ->
"Notepad") and on its process's exe stem. Windows of a shared host process
(``ApplicationFrameHost.exe`` frames every UWP app) are matched on their title
only and closed one window at a time, so closing "Calculator" leaves other UWP
apps open.
"""

from __future__ import annotations

import re
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

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _WM_CLOSE: Final[int] = 0x0010
    _GW_OWNER: Final[int] = 4
    _GWL_EXSTYLE: Final[int] = -20
    _WS_EX_TOOLWINDOW: Final[int] = 0x00000080
    _DWMWA_CLOAKED: Final[int] = 14
    _TITLE_MAX_CHARS: Final[int] = 512

    _SHARED_HOST_EXES: Final[frozenset[str]] = frozenset({"applicationframehost.exe"})
    _OS_SHELL_EXES: Final[frozenset[str]] = frozenset(
        {
            "explorer.exe",
            "shellexperiencehost.exe",
            "startmenuexperiencehost.exe",
            "searchhost.exe",
            "searchapp.exe",
            "textinputhost.exe",
            "lockapp.exe",
            "dwm.exe",
            "csrss.exe",
            "winlogon.exe",
        }
    )
    # Terminals whose tabs close with ctrl+shift+w rather than ctrl+w.
    _TABBED_TERMINAL_EXES: Final[frozenset[str]] = frozenset({"windowsterminal.exe"})
    # Terminal hosts that may hold the runner's console without being its
    # ancestor: Windows 11 hands a console started from Explorer or a shortcut
    # to Windows Terminal, and GetConsoleWindow() then returns a pseudo-window.
    # While the runner has a console, these are treated as hosting it.
    _TERMINAL_HOST_EXES: Final[frozenset[str]] = frozenset(
        {
            "windowsterminal.exe",
            "openconsole.exe",
            "wezterm-gui.exe",
            "alacritty.exe",
            "tabby.exe",
            "hyper.exe",
            "conemu.exe",
            "conemu64.exe",
            "mintty.exe",
            "warp.exe",
        }
    )
    # "Document - App", "Page — Browser": the app name is the last part.
    _TITLE_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"\s[-–—]\s")

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi")

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = wintypes.LONG
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _kernel32.GetConsoleWindow.argtypes = []
    _kernel32.GetConsoleWindow.restype = wintypes.HWND
    _dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    @dataclass(frozen=True)
    class _Window:
        hwnd: int
        title: str
        pid: int
        exe_path: str
        # Lowercased exe file name, e.g. "notepad.exe"; "" when unreadable.
        exe_name: str

        @property
        def shared_host(self) -> bool:
            return self.exe_name in _SHARED_HOST_EXES

        @property
        def app_name(self) -> str:
            name = _TITLE_SEPARATOR.split(self.title)[-1].strip()
            return name or Path(self.exe_path).stem or self.title

    @dataclass(frozen=True)
    class _Target:
        """One process's windows, or a single shared-host window."""

        name: str
        pid: int
        windows: tuple[_Window, ...]

        @property
        def shared_host(self) -> bool:
            return self.windows[0].shared_host

    def _window_title(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(_TITLE_MAX_CHARS)
        _user32.GetWindowTextW(hwnd, buffer, _TITLE_MAX_CHARS)
        return buffer.value

    def _window_pid(hwnd: int) -> int:
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _is_cloaked(hwnd: int) -> bool:
        # Suspended UWP apps keep visible-but-cloaked windows nobody can see.
        cloaked = wintypes.DWORD(0)
        result = _dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return result == 0 and cloaked.value != 0

    def _is_app_window(hwnd: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return False
        if _user32.GetWindow(hwnd, _GW_OWNER):
            return False
        if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
            return False
        return not _is_cloaked(hwnd)

    def _process_exe(pid: int, cache: dict[int, tuple[str, str]]) -> tuple[str, str]:
        cached = cache.get(pid)
        if cached is not None:
            return cached
        exe_path = ""
        exe_name = ""
        try:
            process = psutil.Process(pid)
            exe_name = process.name().lower()
            try:
                exe_path = process.exe()
            except psutil.AccessDenied:
                # Elevated processes still expose their name.
                exe_path = process.name()
        except psutil.Error:
            pass
        cache[pid] = (exe_path, exe_name)
        return exe_path, exe_name

    def _describe(hwnd: int, cache: dict[int, tuple[str, str]]) -> _Window:
        pid = _window_pid(hwnd)
        exe_path, exe_name = _process_exe(pid, cache)
        return _Window(
            hwnd=hwnd,
            title=_window_title(hwnd),
            pid=pid,
            exe_path=exe_path,
            exe_name=exe_name,
        )

    def _app_windows() -> list[_Window]:
        hwnds: list[int] = []

        def collect(hwnd: int | None, _lparam: int) -> bool:
            if hwnd:
                hwnds.append(hwnd)
            return True

        _user32.EnumWindows(_WNDENUMPROC(collect), 0)
        cache: dict[int, tuple[str, str]] = {}
        windows: list[_Window] = []
        for hwnd in hwnds:
            if not _is_app_window(hwnd):
                continue
            window = _describe(hwnd, cache)
            if window.title:
                windows.append(window)
        return windows

    def _window_score(query: str, window: _Window) -> float:
        # A shared host's exe says nothing about the app it frames.
        exec_path = "" if window.shared_host else window.exe_path
        return match_score(query, window.app_name, exec_path)

    def _targets(windows: list[_Window]) -> list[_Target]:
        by_pid: dict[int, list[_Window]] = {}
        targets: list[_Target] = []
        for window in windows:
            if window.shared_host:
                targets.append(_Target(window.app_name, window.pid, (window,)))
            else:
                by_pid.setdefault(window.pid, []).append(window)
        for pid, group in by_pid.items():
            targets.append(_Target(group[0].app_name, pid, tuple(group)))
        return targets

    def _target_score(query: str, target: _Target) -> float:
        return max(_window_score(query, window) for window in target.windows)

    @dataclass(frozen=True)
    class _Console:
        """The runner's console window (possibly a pseudo-window) and its owner."""

        hwnd: int | None
        pid: int | None

    def _runner_console() -> _Console:
        hwnd = _kernel32.GetConsoleWindow()
        if not hwnd:
            return _Console(hwnd=None, pid=None)
        return _Console(hwnd=hwnd, pid=_window_pid(hwnd))

    def _is_runner_window(
        window: _Window, runner: RunnerProcesses, console: _Console
    ) -> bool:
        return (
            runner.is_runner(window.pid)
            or (console.hwnd is not None and window.hwnd == console.hwnd)
            or (console.pid is not None and window.pid == console.pid)
        )

    def _may_host_runner(
        window: _Window, runner: RunnerProcesses, console: _Console
    ) -> bool:
        if runner.hosts_runner(window.pid):
            return True
        # Fail closed: a known terminal host may hold the runner's console
        # through the default-terminal handoff, which leaves no ancestry.
        return console.hwnd is not None and window.exe_name in _TERMINAL_HOST_EXES

    def _protection(
        target: _Target, runner: RunnerProcesses, console: _Console
    ) -> str | None:
        """Why ``target`` must stay open, or None when it may be closed."""
        if any(_may_host_runner(w, runner, console) for w in target.windows):
            return hosts_runner_message(target.name)
        if any(_is_runner_window(w, runner, console) for w in target.windows):
            return refused_message(target.name)
        if target.windows[0].exe_name in _OS_SHELL_EXES:
            return refused_message(target.name)
        return None

    def _windows_gone(targets: list[_Target]) -> bool:
        return not any(
            _user32.IsWindow(w.hwnd) and _user32.IsWindowVisible(w.hwnd)
            for target in targets
            for w in target.windows
        )

    def _still_open(targets: list[_Target]) -> list[str]:
        return [t.name for t in targets if not _windows_gone([t])]

    def _post_close(targets: list[_Target]) -> list[str]:
        """Post WM_CLOSE; return names Windows refused to deliver it to."""
        refused: list[str] = []
        for target in targets:
            delivered = [
                bool(_user32.PostMessageW(w.hwnd, _WM_CLOSE, 0, 0))
                for w in target.windows
            ]
            if not any(delivered):
                refused.append(target.name)
        return refused

    def _force_close(targets: list[_Target]) -> list[str]:
        """Kill each target's process; return names that could not be killed."""
        failed: list[str] = []
        for target in targets:
            try:
                psutil.Process(target.pid).kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.Error:
                failed.append(target.name)
        return failed

    def close_app(query: str, force: bool, runner: RunnerProcesses) -> tuple[bool, str]:
        targets = _targets(_app_windows())
        matches = best_matches(query, targets, _target_score)
        if not matches:
            ranked = [(t.name, _target_score(query, t)) for t in targets]
            return False, no_match_message(query, ranked)
        console = _runner_console()
        allowed: list[_Target] = []
        refusals: list[tuple[_Target, str]] = []
        for target in matches:
            refusal = _protection(target, runner, console)
            if refusal is None:
                allowed.append(target)
            else:
                refusals.append((target, refusal))
        if not allowed:
            return False, refusals[0][1]

        if force:
            hosted = [t.name for t in allowed if t.shared_host]
            if hosted:
                return False, (
                    f"{', '.join(hosted)} runs inside a shared Windows host "
                    "process and cannot be force-closed; handle its dialog, then "
                    "call close_app without force"
                )
            failed = _force_close(allowed)
            if failed:
                return False, (
                    f"Could not force-close {', '.join(failed)}; it may run "
                    "elevated. Run the Test Runner as Administrator"
                )
            timeout = FORCE_CLOSE_TIMEOUT_SECONDS
        else:
            refused = _post_close(allowed)
            if refused:
                return False, (
                    f"Windows refused to send a close request to "
                    f"{', '.join(refused)}; it may run elevated. Run the Test "
                    "Runner as Administrator"
                )
            timeout = GRACEFUL_CLOSE_TIMEOUT_SECONDS

        if not wait_until(lambda: _windows_gone(allowed), timeout):
            still_open = _still_open(allowed)
            if force:
                return False, force_failed_message(still_open)
            return False, still_open_message(still_open)
        return True, closed_message(
            [t.name for t in allowed], [t.name for t, _ in refusals]
        )

    def close_tab(query: str, runner: RunnerProcesses) -> tuple[bool, str]:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False, front_app_mismatch_message("No app", query)
        front = _describe(hwnd, {})
        if _window_score(query, front) < MATCH_THRESHOLD:
            return False, front_app_mismatch_message(front.app_name, query)
        console = _runner_console()
        if _is_runner_window(front, runner, console):
            return False, RUNNER_TAB_REFUSAL
        # Tabs of a terminal that may host the runner cannot be told apart.
        if _may_host_runner(front, runner, console):
            return False, UNKNOWN_TAB_REFUSAL
        keys = "ctrl+shift+w" if front.exe_name in _TABBED_TERMINAL_EXES else "ctrl+w"
        execute_key_press(KeyPressPayload(keys=keys))
        return True, closed_tab_message(front.app_name)
