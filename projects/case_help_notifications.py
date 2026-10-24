"""Tell the project's members that a run case needs help (Web Push)."""

from __future__ import annotations


def notify_case_needs_help(
    help_request_id: int, *, after_auto_fix: bool = False
) -> None:
    """Push "needs help" to every project member with notifications enabled.

    ``after_auto_fix`` selects the "Auto-fix could not fix it" wording. Enqueues
    ``notifications.tasks.send_push`` and never raises.
    """
    raise NotImplementedError
