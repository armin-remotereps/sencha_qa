"""Write a person's Mark failed / Mark blocked verdict (Human in the loop).

The person's decision is final. Normally the worker records the agent's
evidence first (``agents.services.person_mark``) and then writes the verdict
here (``apply_person_mark``); when the run cannot continue, the service
writes it at once without evidence (``write_person_mark_verdict`` +
``log_no_evidence``). The verdict text was written into ``result`` when the
person marked the case. Leaf module: models, ``agents`` and
``case_duration_services`` only (``projects.services`` imports it).
"""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Concat
from django.utils import timezone

from agents.checkpoint import checkpoint_to_json, without_person_step
from agents.types import OrchestratorCheckpoint, PersonMarkVerdict
from projects.case_duration_services import close_case_segment
from projects.models import (
    PERSON_STEP_AGENT_TEXT_MAX_LENGTH,
    BlockedReason,
    CasePersonStep,
    FailedBy,
    PersonStepClose,
    PersonStepStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_rows import cap_text

MARK_CLOSE_REASONS: tuple[str, ...] = (
    PersonStepClose.MARKED_FAILED,
    PersonStepClose.MARKED_BLOCKED,
)

_PIVOT_VERDICT_FIELDS = [
    "status",
    "failed_by",
    "blocked_reason",
    "finished_at",
    "updated_at",
]


def write_person_mark_verdict(
    pivot: TestRunTestCase, verdict: PersonMarkVerdict, now: datetime
) -> list[str]:
    """Set the person's verdict on the locked ``pivot``; the fields to save.

    A person-only Blocked writer (``help_skipped``); the status comes from a
    variable so the single literal Blocked assignment stays in
    ``case_help_services``.
    """
    if verdict == "failed":
        pivot.status = TestRunTestCaseStatus.FAILED
        pivot.failed_by = FailedBy.HELP
        pivot.blocked_reason = ""
    else:
        blocked = TestRunTestCaseStatus.BLOCKED
        pivot.status = blocked
        pivot.blocked_reason = BlockedReason.HELP_SKIPPED
    pivot.finished_at = now
    return list(_PIVOT_VERDICT_FIELDS)


def close_marked_row(
    pivot_id: int,
    person_step_id: int | None,
    verdict: PersonMarkVerdict,
    now: datetime,
    *,
    agent_summary: str = "",
) -> int:
    """Close the marked row (still ``reported`` while evidence is recorded).

    Failed: ``failed``; blocked: ``cancelled`` (the step was not performed;
    the blocked case resumes later from its checkpoint). Guarded by the mark's
    close reason, so no other close is overwritten. The number of rows closed.
    """
    if person_step_id is None:
        return 0
    return CasePersonStep.objects.filter(
        pk=person_step_id,
        test_run_test_case_id=pivot_id,
        status=PersonStepStatus.REPORTED,
        close_reason__in=MARK_CLOSE_REASONS,
    ).update(
        status=(
            PersonStepStatus.FAILED
            if verdict == "failed"
            else PersonStepStatus.CANCELLED
        ),
        closed_at=now,
        # Without evidence the row keeps the summary it had.
        **(
            {
                "agent_summary": cap_text(
                    agent_summary, PERSON_STEP_AGENT_TEXT_MAX_LENGTH
                )
            }
            if agent_summary.strip()
            else {}
        ),
    )


def apply_person_mark(
    pivot: TestRunTestCase,
    verdict: PersonMarkVerdict,
    checkpoint: OrchestratorCheckpoint | None,
    *,
    person_step_id: int | None,
    generation: int,
) -> bool:
    """The worker writes the person's verdict after the evidence pass.

    ``checkpoint`` (the evidence attempt appended and the ``person`` key
    cleared) is written with it; None keeps the stored one without its
    ``person`` key. Only while the
    locked case is still In progress in ``generation``: a Needs re-test mark,
    an abort or a newer chain that landed first is kept. True when written.
    """
    with transaction.atomic():
        # The person step before the case: every person step writer's order.
        if person_step_id is not None:
            CasePersonStep.objects.select_for_update().filter(
                pk=person_step_id, test_run_test_case_id=pivot.pk
            ).first()
        locked = (
            TestRunTestCase.objects.select_for_update(of=("self",))
            .defer("logs")
            .filter(pk=pivot.pk, test_run__execution_generation=generation)
            .first()
        )
        if locked is None or locked.status != TestRunTestCaseStatus.IN_PROGRESS:
            return False
        now = timezone.now()
        fields = write_person_mark_verdict(locked, verdict, now)
        locked.execution_checkpoint = (
            checkpoint_to_json(checkpoint)
            if checkpoint is not None
            else without_person_step(locked.execution_checkpoint)
        )
        fields.append("execution_checkpoint")
        locked.save(update_fields=fields)
        close_case_segment(locked.pk, now)
        summary = ""
        if checkpoint is not None and checkpoint.attempts:
            newest = checkpoint.attempts[-1]
            if newest.person_step_id == person_step_id:
                summary = newest.result.summary
        close_marked_row(locked.pk, person_step_id, verdict, now, agent_summary=summary)
    pivot.status = locked.status
    pivot.failed_by = locked.failed_by
    pivot.blocked_reason = locked.blocked_reason
    pivot.result = locked.result
    pivot.finished_at = locked.finished_at
    pivot.segment_started_at = None
    return True


def settle_person_marks_on_abort(
    test_run_id: int, now: datetime
) -> list[TestRunTestCase]:
    """The run is aborted while a person's mark waits for its evidence pass:
    the person's verdict stands, without evidence.

    Called by the abort under the run lock, before it cancels the open person
    steps and cases (person step, then case). A run has at most one such case.
    The settled cases, for the caller to broadcast.
    """
    rows = list(
        CasePersonStep.objects.select_for_update(of=("self",))
        .filter(
            test_run_test_case__test_run_id=test_run_id,
            status=PersonStepStatus.REPORTED,
            close_reason__in=MARK_CLOSE_REASONS,
        )
        .values_list("pk", "test_run_test_case_id", "close_reason")
    )
    settled: list[TestRunTestCase] = []
    for person_step_id, pivot_id, close_reason in rows:
        pivot = (
            TestRunTestCase.objects.select_for_update()
            .defer("logs")
            .filter(
                pk=pivot_id,
                status__in=(
                    TestRunTestCaseStatus.CREATED,
                    TestRunTestCaseStatus.IN_PROGRESS,
                ),
            )
            .first()
        )
        if pivot is None:
            continue
        verdict: PersonMarkVerdict = (
            "failed" if close_reason == PersonStepClose.MARKED_FAILED else "blocked"
        )
        fields = write_person_mark_verdict(pivot, verdict, now)
        pivot.execution_checkpoint = without_person_step(pivot.execution_checkpoint)
        pivot.save(update_fields=[*fields, "execution_checkpoint"])
        # A Created case has no open segment; the close is idempotent.
        close_case_segment(pivot.pk, now)
        close_marked_row(pivot.pk, person_step_id, verdict, now)
        log_no_evidence(pivot.pk, "the run was stopped")
        pivot.segment_started_at = None
        settled.append(pivot)
    return settled


def log_no_evidence(pivot_id: int, reason: str) -> None:
    """One case log line saying the verdict stands without evidence."""
    TestRunTestCase.objects.filter(pk=pivot_id).update(
        logs=Concat(F("logs"), Value(f"[Evidence] no evidence: {reason}\n")),
        updated_at=timezone.now(),
    )
