"""Which running apps close_app / close_tab may touch, and what they reply.

Platform modules (``app_closer_macos``, ``app_closer_windows``) list and close
apps; this module holds the rules they share: how an app name is matched, how
the Test Runner's own processes are identified so they are never closed, and
the reply texts the agent acts on.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

import psutil

from controller_client.app_discovery import (
    MATCH_THRESHOLD,
    AppCandidate,
    compute_match_score,
)
from controller_client.exceptions import ExecutionError

GRACEFUL_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
FORCE_CLOSE_TIMEOUT_SECONDS: Final[float] = 5.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.2
_MAX_LISTED_APPS: Final[int] = 5

RUNNER_TAB_REFUSAL: Final[str] = "That tab runs the Test Runner"
UNKNOWN_TAB_REFUSAL: Final[str] = (
    "cannot tell whether the front tab runs the Test Runner; type `exit` in "
    "the tab you opened instead"
)

T = TypeVar("T")


@dataclass(frozen=True)
class RunnerProcesses:
    """The Test Runner's own process, the processes hosting it, and its tty."""

    own_pid: int
    ancestor_pids: frozenset[int]
    # Controlling terminal (e.g. "/dev/ttys003"); None when there is none or
    # on Windows, where terminals have no tty.
    terminal: str | None

    def is_runner(self, pid: int) -> bool:
        return pid == self.own_pid

    def hosts_runner(self, pid: int) -> bool:
        return pid in self.ancestor_pids


def current_runner_processes() -> RunnerProcesses:
    """Read the runner's process ancestry; fail closed when it is unreadable."""
    try:
        me = psutil.Process()
        ancestors = me.parents()
        return RunnerProcesses(
            own_pid=me.pid,
            ancestor_pids=frozenset(a.pid for a in ancestors),
            terminal=_controlling_terminal([me, *ancestors]),
        )
    except psutil.Error as e:
        raise ExecutionError(
            f"Cannot identify the Test Runner's own processes, so nothing is "
            f"closed: {e}"
        ) from e


if sys.platform == "win32":

    def _controlling_terminal(processes: Sequence[psutil.Process]) -> str | None:
        return None

else:

    def _controlling_terminal(processes: Sequence[psutil.Process]) -> str | None:
        # The runner itself may have its stdin redirected; its shell still
        # owns the terminal tab it was started from.
        for process in processes:
            try:
                terminal: str | None = process.terminal()
            except psutil.Error:
                continue
            if terminal:
                return terminal
        return None


def match_score(query: str, name: str, exec_path: str) -> float:
    return compute_match_score(
        query, AppCandidate(display_name=name, exec_path=exec_path)
    )


def best_matches(
    query: str, items: Sequence[T], score: Callable[[str, T], float]
) -> list[T]:
    """Items tied for the best score, or none when it is under the threshold.

    Only the best match is acted on, so "Code" closes Xcode or VS Code, not both,
    and a weaker match never stands in for a protected best one.
    """
    scored = [(item, score(query, item)) for item in items]
    best = max((value for _, value in scored), default=0.0)
    if best < MATCH_THRESHOLD:
        return []
    return [item for item, value in scored if value == best]


def no_match_message(query: str, ranked: Sequence[tuple[str, float]]) -> str:
    """Name the running apps that come closest to ``query``."""
    names: list[str] = []
    for name, _ in sorted(ranked, key=lambda pair: (-pair[1], pair[0].lower())):
        if name and name not in names:
            names.append(name)
        if len(names) == _MAX_LISTED_APPS:
            break
    listed = ", ".join(names) if names else "none found"
    return f"No running app matches '{query}'. Running apps: {listed}"


def refused_message(name: str) -> str:
    return f"Refused to close {name}: that is the Test Runner or the OS shell"


def hosts_runner_message(name: str) -> str:
    return (
        f"{name} hosts the Test Runner; close only the tab you opened with " "close_tab"
    )


def still_open_message(names: Sequence[str]) -> str:
    return (
        f"{', '.join(names)} is still open; it may be showing a save or confirm "
        "dialog. Handle the dialog, or call close_app with force=true"
    )


def force_failed_message(names: Sequence[str]) -> str:
    return f"{', '.join(names)} is still open after a forced close"


def closed_message(names: Sequence[str], left_open: Sequence[str]) -> str:
    message = f"Closed {', '.join(names)}"
    if left_open:
        message += (
            f" (left {', '.join(left_open)} open: it is the Test Runner, hosts "
            "it, or is the OS shell)"
        )
    return message


def front_app_mismatch_message(front_name: str, query: str) -> str:
    return (
        f"{front_name} is in front, not {query}; click the tab you want to close "
        "first"
    )


def closed_tab_message(name: str) -> str:
    return f"Closed the front tab of {name}"


def wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)
