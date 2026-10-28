from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import psutil


class ProcessTracker:
    def __init__(self) -> None:
        # pid -> create time, so a PID the OS reused after our process exited
        # is never killed (taskkill /T would take its whole tree with it).
        self._pids: dict[int, float] = {}
        self._lock = threading.Lock()

    def register(self, pid: int) -> None:
        created = _create_time(pid)
        if created is None:
            return
        with self._lock:
            self._pids[pid] = created

    def kill_all(self) -> list[int]:
        with self._lock:
            snapshot = dict(self._pids)
            self._pids.clear()

        alive = {
            pid for pid, created in snapshot.items() if _create_time(pid) == created
        }
        if not alive:
            return []

        return _kill_all(alive)


def _create_time(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


if sys.platform == "win32":

    def kill_process_tree(pid: int) -> bool:
        """Kill ``pid`` and its children; return whether it worked."""
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
        return result.returncode == 0

    def _kill_all(pids: set[int]) -> list[int]:
        return [pid for pid in pids if kill_process_tree(pid)]

else:

    def _kill_all(pids: set[int]) -> list[int]:
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
