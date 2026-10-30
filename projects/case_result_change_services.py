"""Use cases for a result-chat change of a finished case's result.

The result-chat agent proposes a new status with a new result summary
(``propose_result_change``); it is stored as the case's single pending change.
A person applies it (``apply_result_change``) or discards it
(``discard_result_change``). Nothing changes until a person applies it.

Writes lock the project, then the run, then the case, then the change (the
order `start_test_run` and the help writers use). A change is built on a
fingerprint of the case's result; any later re-run, resume or result change
makes it outdated, and Apply refuses it.

Apply is the second person-only writer of ``BLOCKED`` (after Mark blocked), with
its own reason ``PERSON_REVIEW``; the agent only proposes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.case_result_text import (
    format_changed_result,
    person_label,
    result_status_label,
)
from projects.exceptions import ResultChangeError
from projects.models import (
    RESULT_CHANGE_EXPLANATION_MAX_LENGTH,
    RESULT_CHANGE_SUMMARY_MAX_LENGTH,
    BlockedReason,
    CaseResultChange,
    FailedBy,
    ResultChangeStatus,
    TestRun,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_reopen_services import (
    CHECKED_SENT_FIELDS,
    clear_checked_and_sent,
    list_testrail_push_blockers,
    lock_project_and_run,
)
from projects.services import broadcast_run_and_pivots
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

RESULT_CHANGEABLE_STATUSES: tuple[str, ...] = (
    TestRunTestCaseStatus.SUCCESS,
    TestRunTestCaseStatus.FAILED,
    TestRunTestCaseStatus.BLOCKED,
)

NOT_CHANGEABLE = (
    "Only the result of a Passed, Failed or Blocked case can be changed; this "
    "case is not finished (it may be running again)."
)
CASE_CHANGED = (
    "The case changed since this was proposed: it was re-run, resumed or its "
    "result changed. Ask the agent again."
)
_CASE_GONE = "This case no longer exists."
_INVALID_STATUS = "Choose Passed, Failed or Blocked."
_EMPTY_SUMMARY = "Write the new result summary."
_CHANGE_GONE = "This result change no longer exists."
_CHANGE_NOT_PENDING = "This result change is no longer pending. Reload the page."

_STATUS_LABELS: dict[str, str] = {
    ResultChangeStatus.PROPOSED: "Proposed",
    ResultChangeStatus.APPLIED: "Applied",
    ResultChangeStatus.DISCARDED: "Discarded",
    ResultChangeStatus.SUPERSEDED: "Replaced",
}


@dataclass(frozen=True)
class ResultChangeView:
    """A result change card's display data; every text is shown escaped."""

    change_id: int
    # ``ResultChangeStatus`` value.
    status: str
    status_label: str
    from_label: str
    to_label: str
    # Model-written.
    summary: str
    explanation: str
    # Server-built: what Apply does.
    effects: str
    # Pending, but the case changed since it was proposed (Apply refuses it).
    outdated: bool
    # Who applied or discarded it; "" when nobody (yet) or a removed user.
    decided_by_name: str


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def is_result_changeable(pivot: TestRunTestCase) -> bool:
    """Whether the case has a result a person may change (no query)."""
    return pivot.status in RESULT_CHANGEABLE_STATUSES


def result_fingerprint(pivot: TestRunTestCase) -> str:
    """sha256 of the case's status, blocked reason, finish time and result."""
    canonical = [
        pivot.status,
        pivot.blocked_reason,
        pivot.finished_at.isoformat() if pivot.finished_at else "",
        pivot.result,
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def get_result_change_view(
    change: CaseResultChange, pivot: TestRunTestCase
) -> ResultChangeView:
    """The card of ``change`` against the case as it is now (pure).

    ``change.decided_by`` should be loaded (``select_related``).
    """
    outdated = change.status == ResultChangeStatus.PROPOSED and (
        not is_result_changeable(pivot)
        or result_fingerprint(pivot) != change.base_fingerprint
    )
    decided_by = change.decided_by
    return ResultChangeView(
        change_id=change.pk,
        status=change.status,
        status_label=_STATUS_LABELS.get(change.status, change.status),
        from_label=result_status_label(change.from_status),
        to_label=result_status_label(change.to_status),
        summary=change.summary,
        explanation=change.explanation,
        effects=_effects(change.to_status),
        outdated=outdated,
        decided_by_name=person_label(decided_by),
    )


def list_result_changes(pivot: TestRunTestCase) -> list[CaseResultChange]:
    """The case's applied result changes, newest first, with who applied them.

    One query; these rows are the audit trail of a person's changes.
    """
    return list(
        CaseResultChange.objects.filter(
            test_run_test_case_id=pivot.pk, status=ResultChangeStatus.APPLIED
        )
        .select_related("decided_by")
        .order_by("-decided_at", "-id")
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def propose_result_change(
    *,
    pivot_id: int,
    to_status: str,
    summary: str,
    explanation: str,
    conversation_id: int | None,
) -> CaseResultChange:
    """Store a proposed result change as the case's single pending change.

    ``to_status`` is a ``TestRunTestCaseStatus`` value (success / failed /
    blocked). Raises ``ResultChangeError`` with a message the agent can act on.
    """
    summary = summary.strip()[:RESULT_CHANGE_SUMMARY_MAX_LENGTH]
    explanation = explanation.strip()[:RESULT_CHANGE_EXPLANATION_MAX_LENGTH]
    if to_status not in RESULT_CHANGEABLE_STATUSES:
        raise ResultChangeError(_INVALID_STATUS)
    if not summary:
        raise ResultChangeError(_EMPTY_SUMMARY)
    with transaction.atomic():
        _, pivot = _lock_case(pivot_id)
        if not is_result_changeable(pivot):
            raise ResultChangeError(NOT_CHANGEABLE)
        if to_status == pivot.status:
            raise ResultChangeError(
                f"The case is already {result_status_label(to_status)}; propose a "
                "different status."
            )
        _supersede_pending(pivot.pk)
        return CaseResultChange.objects.create(
            test_run_test_case=pivot,
            base_fingerprint=result_fingerprint(pivot),
            from_status=pivot.status,
            from_blocked_reason=pivot.blocked_reason,
            from_result=pivot.result,
            to_status=to_status,
            summary=summary,
            explanation=explanation,
            conversation_id=conversation_id,
        )


def apply_result_change(
    *, pivot: TestRunTestCase, change_id: int, user: CustomUser
) -> None:
    """A person applies the pending change: the case gets its new status and result.

    Clears the case's Checked/Sent marks (a new TestRail result can be sent)
    and keeps its finish time and checkpoint. Run counts are computed, and a
    finished-to-finished change needs no run write. Raises
    ``ResultChangeError`` (user-safe).
    """
    with transaction.atomic():
        test_run, locked = _lock_case(pivot.pk)
        change = (
            CaseResultChange.objects.select_for_update()
            .filter(pk=change_id, test_run_test_case_id=locked.pk)
            .first()
        )
        if change is None:
            raise ResultChangeError(_CHANGE_GONE)
        if change.status != ResultChangeStatus.PROPOSED:
            raise ResultChangeError(_CHANGE_NOT_PENDING)
        if result_fingerprint(locked) != change.base_fingerprint:
            raise ResultChangeError(CASE_CHANGED)
        if not is_result_changeable(locked):
            raise ResultChangeError(NOT_CHANGEABLE)
        push_blockers = list_testrail_push_blockers(test_run)
        if push_blockers:
            raise ResultChangeError(push_blockers[0])

        now = timezone.now()
        from_status = locked.status
        from_blocked_reason = locked.blocked_reason
        from_result = locked.result
        new_status: str = change.to_status
        locked.status = new_status
        locked.blocked_reason = (
            BlockedReason.PERSON_REVIEW
            if new_status == TestRunTestCaseStatus.BLOCKED
            else ""
        )
        update_fields = ["status", "blocked_reason", "result"]
        if new_status == TestRunTestCaseStatus.FAILED:
            locked.failed_by = FailedBy.REVIEW
            update_fields.append("failed_by")
        locked.result = format_changed_result(
            from_status=from_status,
            to_status=new_status,
            person=person_label(user),
            changed_at=timezone.localtime(now),
            summary=change.summary,
        )
        clear_checked_and_sent(locked)
        locked.save(update_fields=[*update_fields, *CHECKED_SENT_FIELDS, "updated_at"])
        record_case_activity(
            user=user, pivot_ids=[locked.pk], activity=CaseActivity.RESULT_CHANGE
        )

        change.from_status = from_status
        change.from_blocked_reason = from_blocked_reason
        change.from_result = from_result
        change.status = ResultChangeStatus.APPLIED
        change.decided_by = user
        change.decided_at = now
        change.save(
            update_fields=[
                "from_status",
                "from_blocked_reason",
                "from_result",
                "status",
                "decided_by",
                "decided_at",
            ]
        )
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [locked]))


def discard_result_change(
    *, pivot: TestRunTestCase, change_id: int, user: CustomUser
) -> None:
    """A person rejects the pending change; the agent may propose another."""
    updated = CaseResultChange.objects.filter(
        pk=change_id,
        test_run_test_case_id=pivot.pk,
        status=ResultChangeStatus.PROPOSED,
    ).update(
        status=ResultChangeStatus.DISCARDED,
        decided_by=user,
        decided_at=timezone.now(),
    )
    if not updated:
        raise ResultChangeError(_CHANGE_NOT_PENDING)


def supersede_pending_result_change(pivot_id: int) -> None:
    """Retire the case's pending change (a newer one, or the case re-runs)."""
    _supersede_pending(pivot_id)


def supersede_pending_result_changes(pivot_ids: Sequence[int]) -> None:
    """Retire the pending changes of several cases in one UPDATE."""
    CaseResultChange.objects.filter(
        test_run_test_case_id__in=pivot_ids, status=ResultChangeStatus.PROPOSED
    ).update(status=ResultChangeStatus.SUPERSEDED, decided_at=timezone.now())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lock_case(pivot_id: int) -> tuple[TestRun, TestRunTestCase]:
    """Lock project, run and case; must run inside ``transaction.atomic()``."""
    test_run_id = (
        TestRunTestCase.objects.filter(pk=pivot_id)
        .values_list("test_run_id", flat=True)
        .first()
    )
    if test_run_id is None:
        raise ResultChangeError(_CASE_GONE)
    _, test_run = lock_project_and_run(test_run_id)
    # Logs can be large and are never touched here.
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .defer("logs")
        .filter(pk=pivot_id)
        .first()
    )
    if pivot is None:
        raise ResultChangeError(_CASE_GONE)
    return test_run, pivot


def _supersede_pending(pivot_id: int) -> None:
    CaseResultChange.objects.filter(
        test_run_test_case_id=pivot_id, status=ResultChangeStatus.PROPOSED
    ).update(status=ResultChangeStatus.SUPERSEDED, decided_at=timezone.now())


def _effects(to_status: str) -> str:
    """What Apply does, from the target status only (no user or model text)."""
    if to_status == TestRunTestCaseStatus.BLOCKED:
        outcome = (
            "Marks the case Blocked (it could not be judged; it can be resumed "
            "later)"
        )
    else:
        outcome = f"Sets the case to {result_status_label(to_status)}"
    return (
        f"{outcome} and replaces its result text with this summary, under a line "
        "naming you. The earlier result is kept in the case's history. Checked "
        "and Sent marks on this case are cleared, so a new TestRail result can "
        "be sent."
    )
