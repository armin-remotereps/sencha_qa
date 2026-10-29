"""The effective outcome of a session case, and its display labels.

A ``TestSessionCase`` row follows its live run case until it is frozen
(``frozen_at`` set); from then on its ``frozen_*`` columns hold the outcome.
The expressions below pick one or the other in SQL, so the pages and the CSV
export read the same values in one query (only the pivot is joined). The
label helpers are pure mappings of the ``projects.models`` choices.
"""

from __future__ import annotations

from django.db.models import (
    Case,
    CharField,
    DateTimeField,
    DurationField,
    F,
    Q,
    TextField,
    Value,
    When,
)
from django.db.models.expressions import Combinable

from projects.models import (
    FINISHED_CASE_STATUSES,
    BlockedReason,
    FailedBy,
    RunHelpMode,
    TestRunTestCaseStatus,
)
from test_sessions.case_activity import CaseActivity

# Annotation names used by ``outcome_annotations`` (and read back by callers).
STATUS = "outcome_status"
FAILED_BY = "outcome_failed_by"
BLOCKED_REASON = "outcome_blocked_reason"
STARTED_AT = "outcome_started_at"
FINISHED_AT = "outcome_finished_at"
DURATION = "outcome_duration"
RESULT = "outcome_result"

NOT_AVAILABLE_LABEL = "Not available"
PERSON_LABEL = "Person (Human in the loop)"
AGENT_LABEL = "AI agent"

_FROZEN = Q(frozen_at__isnull=False)
_LIVE = "test_run_test_case__"

_STATUS_LABELS: dict[str, str] = dict(TestRunTestCaseStatus.choices)
_FAILED_BY_LABELS: dict[str, str] = dict(FailedBy.choices)
_BLOCKED_REASON_LABELS: dict[str, str] = dict(BlockedReason.choices)
_ACTIVITY_LABELS: dict[str, str] = dict(CaseActivity.choices)


def _frozen_or_live(
    field: str, output_field: CharField[str, str] | TextField[str, str]
) -> Case:
    return Case(
        When(_FROZEN, then=F(f"frozen_{field}")),
        default=F(f"{_LIVE}{field}"),
        output_field=output_field,
    )


def _frozen_or_live_time(field: str) -> Case:
    return Case(
        When(_FROZEN, then=F(f"frozen_{field}")),
        default=F(f"{_LIVE}{field}"),
        output_field=DateTimeField(),
    )


def effective_status() -> Case:
    """The frozen status, else the live pivot's (NULL: pivot gone, not frozen)."""
    return _frozen_or_live("status", CharField())


def effective_duration() -> Case:
    """The frozen duration, else the live ``active_duration`` only once the
    live case has finished (as ``TestRunTestCase.shown_duration``)."""
    return Case(
        When(_FROZEN, then=F("frozen_active_duration")),
        When(
            Q(
                **{
                    f"{_LIVE}status__in": sorted(
                        str(status) for status in FINISHED_CASE_STATUSES
                    )
                }
            ),
            then=F(f"{_LIVE}active_duration"),
        ),
        default=Value(None, output_field=DurationField()),
        output_field=DurationField(),
    )


def outcome_annotations(*, with_result: bool = False) -> dict[str, Combinable]:
    """``annotate()`` keyword arguments for the effective outcome columns.
    ``with_result`` adds the verdict text (the export; never the pages)."""
    annotations: dict[str, Combinable] = {
        STATUS: effective_status(),
        FAILED_BY: _frozen_or_live("failed_by", CharField()),
        BLOCKED_REASON: _frozen_or_live("blocked_reason", CharField()),
        STARTED_AT: _frozen_or_live_time("started_at"),
        FINISHED_AT: _frozen_or_live_time("finished_at"),
        DURATION: effective_duration(),
    }
    if with_result:
        annotations[RESULT] = _frozen_or_live("result", TextField())
    return annotations


def status_label(status: str | None) -> str:
    """The status choice label; "Not available" when unknown or missing."""
    if not status:
        return NOT_AVAILABLE_LABEL
    return _STATUS_LABELS.get(status, NOT_AVAILABLE_LABEL)


def failed_by_label(status: str | None, failed_by: str | None) -> str:
    """Who made the case Failed; empty unless the status is Failed."""
    if status != TestRunTestCaseStatus.FAILED or not failed_by:
        return ""
    return _FAILED_BY_LABELS.get(failed_by, "")


def blocked_reason_label(status: str | None, blocked_reason: str | None) -> str:
    """Why the case is Blocked; empty unless the status is Blocked."""
    if status != TestRunTestCaseStatus.BLOCKED or not blocked_reason:
        return ""
    return _BLOCKED_REASON_LABELS.get(blocked_reason, "")


def performed_by_label(run_help_mode: str) -> str:
    """Who performed the steps, from the run's help mode at the last activity."""
    if run_help_mode == RunHelpMode.HUMAN_IN_THE_LOOP:
        return PERSON_LABEL
    return AGENT_LABEL


def actions_label(first_activity: str, last_activity: str) -> str:
    """The tester's first and last action, e.g. "Started, Resumed" (once each)."""
    labels: list[str] = []
    for activity in (first_activity, last_activity):
        label = _ACTIVITY_LABELS.get(activity, activity)
        if label and label not in labels:
            labels.append(label)
    return ", ".join(labels)
