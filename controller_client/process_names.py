"""Normalized process names of window owners, looked up by pid.

Shared by the Linux window inventories (X11, GNOME, Sway / Hyprland): the
psutil name of the pid, normalized with ``protocol.normalize_process_name``,
cached per inventory. A pid that cannot be read gives "" (never matches an
allowlist entry).
"""

from __future__ import annotations

import psutil

from controller_client.protocol import normalize_process_name


class ProcessNames:
    """Pid to normalized process name, cached for one inventory."""

    def __init__(self) -> None:
        self._cache: dict[int, str] = {}

    def name(self, pid: int | None) -> str:
        """The normalized name of ``pid``; "" for None, a non-positive pid,
        a vanished process or an unusable name."""
        if pid is None or pid <= 0:
            return ""
        cached = self._cache.get(pid)
        if cached is not None:
            return cached
        try:
            raw = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            raw = ""
        name = (normalize_process_name(raw) or "") if raw else ""
        self._cache[pid] = name
        return name
