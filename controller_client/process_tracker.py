from __future__ import annotations

import os
import platform
import signal
import subprocess
import threading
import time


class ProcessTracker:
    def __init__(self) -> None:
        self._pids: set[int] = set()
        self._lock = threading.Lock()

    def register(self, pid: int) -> None:
        with self._lock:
            self._pids.add(pid)

    def kill_all(self) -> list[int]:
        with self._lock:
            snapshot = set(self._pids)
            self._pids.clear()

        if not snapshot:
            return []

        if platform.system() == "Windows":
            return _kill_all_windows(snapshot)
        return _kill_all_posix(snapshot)


def _kill_all_posix(pids: set[int]) -> list[int]:
    killed: list[int] = []
    sigterm_sent: list[int] = []

    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            killed.append(pid)
            sigterm_sent.append(pid)
        except (ProcessLookupError, PermissionError):
            pass

    if sigterm_sent:
        time.sleep(2)

    for pid in sigterm_sent:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    return killed


def _kill_all_windows(pids: set[int]) -> list[int]:
    killed: list[int] = []
    for pid in pids:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
        if result.returncode == 0:
            killed.append(pid)
    return killed
