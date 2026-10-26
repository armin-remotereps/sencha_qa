"""Server-written texts of a person's result change (pure: no ORM).

The applied result keeps a server header naming the person and the earlier
status, so a reviewer (and TestRail) can tell that a person changed the
agent's verdict; the agent's summary follows as the new result text.
"""

from __future__ import annotations

from datetime import datetime

from accounts.models import CustomUser
from projects.models import TestRunTestCaseStatus

# Result statuses as the case page and TestRail name them ("Success" is the
# model label).
_RESULT_STATUS_LABELS: dict[str, str] = {
    TestRunTestCaseStatus.SUCCESS: "Passed",
    TestRunTestCaseStatus.FAILED: "Failed",
    TestRunTestCaseStatus.BLOCKED: "Blocked",
}


def result_status_label(status: str) -> str:
    """ "Passed" / "Failed" / "Blocked"; the model label for any other status."""
    label = _RESULT_STATUS_LABELS.get(status)
    if label is not None:
        return label
    try:
        return str(TestRunTestCaseStatus(status).label)
    except ValueError:
        return status


def person_label(user: CustomUser | None) -> str:
    """The person's full name, or their email when they have none; "" for None.

    Matches how help actions name a person in the case's result text.
    """
    if user is None:
        return ""
    return user.get_full_name() or user.email


def format_changed_result(
    *,
    from_status: str,
    to_status: str,
    person: str,
    changed_at: datetime,
    summary: str,
) -> str:
    """The new result text: the server header, a blank line, the agent's summary.

    ``changed_at`` is shown as given (pass a local time).
    """
    header = (
        f"Result changed from {result_status_label(from_status)} to "
        f"{result_status_label(to_status)} by {person} on "
        f"{format_change_date(changed_at)} after review with the agent."
    )
    summary = summary.strip()
    return f"{header}\n\n{summary}" if summary else header


def format_change_date(moment: datetime) -> str:
    """ "Oct 26, 2026 14:05" (no locale or platform-specific directives)."""
    return f"{moment:%b} {moment.day}, {moment:%Y %H:%M}"
