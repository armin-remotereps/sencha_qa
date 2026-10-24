"""Which finished cases get the teardown that closes the apps they left open.

Pure: no ORM. A passing case closes its apps itself, and an aborted one was
stopped on purpose. A Blocked case only qualifies when the runner, the visual
engine and the AI service still work:

- ``runner_disconnected`` / ``runner_unresponsive``: there is no runner to drive.
- ``visual_engine``: nothing can read the screen.
- ``ai_service``: there is no model to run the teardown.
- ``time_limit``: the hard kill is 60 s away; that time belongs to the cleanup.

A Need help case gets no teardown (and no cleanup): the machine is kept as it
is for the person or auto-fix agent who fixes it, and the case re-runs its
failing step from there. ``help_skipped`` (Skip after help) is not set by a
worker, so it never reaches the teardown either.
"""

from __future__ import annotations

from typing import Final

from projects.models import BlockedReason, TestRunTestCaseStatus

TEARDOWN_BLOCKED_REASONS: Final = frozenset(
    {
        BlockedReason.BROWSER_UNAVAILABLE,
        BlockedReason.RUNNER_OUTDATED,
        BlockedReason.RESUME_STATE_LOST,
        BlockedReason.INTERNAL_ERROR,
    }
)


def needs_teardown(status: str, blocked_reason: str | None) -> bool:
    if status == TestRunTestCaseStatus.FAILED:
        return True
    return (
        status == TestRunTestCaseStatus.BLOCKED
        and blocked_reason in TEARDOWN_BLOCKED_REASONS
    )
