"""Human in the loop person steps as the Ask AI advisor's step and earlier steps.

The HITL mapper onto ``projects.advice_context.build_advice_context``: the
waiting ``CasePersonStep`` becomes an ``AdviceStep(source="plan")`` and the
current plan's earlier person steps become one-line ``AdviceEarlierStep`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from django.db.models import DateTimeField, Subquery, Value
from django.db.models.functions import Coalesce

from agents.types import AdviceCaseStep, AdviceEarlierStep, AdviceStep
from projects.case_step_refs import stored_case_steps
from projects.models import (
    CaseHelpRequest,
    CasePersonStep,
    CasePlanRevision,
    HelpContinueMode,
    HelpResolution,
    PersonStepKind,
    PersonStepOutcome,
    PersonStepStatus,
    PlanRevisionStatus,
    TestRunTestCase,
)
from projects.person_step_rows import stored_hand_off_kind, stored_how_to

# Earlier steps the advisor sees (newest row per step), and their text caps.
EARLIER_STEPS_MAX_ITEMS: Final = 20
# Rows read for them (a step has one row per execution segment, rarely more).
EARLIER_STEP_ROWS_MAX: Final = 60
EARLIER_INSTRUCTION_MAX_CHARS: Final = 160
EARLIER_SUMMARY_MAX_CHARS: Final = 200
CARRIED_STEP_SUMMARY: Final = (
    "Carried from an earlier plan, where it passed; not re-run under this plan."
)
# The lower bound when nothing newer is known (no start, help or redesign).
_EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)
_STATUS_LABELS: Final[dict[str, str]] = {
    str(value): str(label) for value, label in PersonStepStatus.choices
}
_OUTCOME_LABELS: Final[dict[str, str]] = {
    str(value): str(label) for value, label in PersonStepOutcome.choices
}


@dataclass(frozen=True)
class CurrentPlanStart:
    """When the case's current plan began, and how many steps it carried."""

    since: datetime
    # Leading steps carried from the earlier plan when the bound is an applied
    # redesign; 0 otherwise.
    carried_count: int


def advice_step(step: CasePersonStep) -> AdviceStep:
    """The HITL person step as the advisor's step (no queries).

    The checkpoint (with the plan's size) is not loaded, so the label has no
    "of M".
    """
    label = f"Plan step {step.step_num}"
    if step.case_steps_label:
        label = f"{label} · {step.case_steps_label}"
    return AdviceStep(
        source="plan",
        kind=stored_hand_off_kind(step.kind),
        step_num=step.step_num,
        label=label,
        instruction=step.instruction,
        expected_result=step.expected_result,
        command=step.command,
        objection=step.agent_objection,
        how_to=stored_how_to(step.how_to),
        covered_case_steps=tuple(
            AdviceCaseStep(num=item.num, text=item.text, expected=item.expected)
            for item in stored_case_steps(step.case_steps)
        ),
    )


def current_plan_start(pivot: TestRunTestCase) -> CurrentPlanStart:
    """The newest of ``pivot.started_at`` (last from-scratch start), the last
    help request continued "from scratch" and the last applied plan redesign
    (one query).

    ``pivot.started_at`` is not reset by those two, so without them rows of an
    abandoned execution or an earlier plan would count as this plan's steps.
    """
    help_requests = CaseHelpRequest.objects.filter(
        test_run_test_case_id=pivot.pk,
        continue_mode=HelpContinueMode.FROM_SCRATCH,
        resolution__in=(HelpResolution.CONTINUED, HelpResolution.AUTO_FIXED),
        resolved_at__isnull=False,
    ).order_by("-resolved_at")
    revisions = CasePlanRevision.objects.filter(
        test_run_test_case_id=pivot.pk,
        status=PlanRevisionStatus.APPLIED,
        decided_at__isnull=False,
    ).order_by("-decided_at", "-pk")
    epoch = Value(_EPOCH, output_field=DateTimeField())
    bounds = (
        TestRunTestCase.objects.filter(pk=pivot.pk)
        .annotate(
            from_scratch_at=Coalesce(
                Subquery(
                    help_requests.values("resolved_at")[:1],
                    output_field=DateTimeField(),
                ),
                epoch,
            ),
            redesigned_at=Coalesce(
                Subquery(
                    revisions.values("decided_at")[:1], output_field=DateTimeField()
                ),
                epoch,
            ),
            redesign_resume_step=Subquery(revisions.values("resume_step")[:1]),
        )
        .values_list("from_scratch_at", "redesigned_at", "redesign_resume_step")
        .first()
    )
    started_at = pivot.started_at or _EPOCH
    if bounds is None:
        return CurrentPlanStart(since=started_at, carried_count=0)
    from_scratch_at, redesigned_at, resume_step = bounds
    if redesigned_at > max(started_at, from_scratch_at):
        carried = resume_step - 1 if isinstance(resume_step, int) else 0
        return CurrentPlanStart(since=redesigned_at, carried_count=max(0, carried))
    return CurrentPlanStart(since=max(started_at, from_scratch_at), carried_count=0)


def earlier_steps(
    pivot: TestRunTestCase, *, before_step_num: int
) -> tuple[AdviceEarlierStep, ...]:
    """One line per earlier plan step of the current plan (two queries).

    The newest non-restore row of each step below ``before_step_num`` handed
    out since ``current_plan_start``; steps carried by the redesign that
    started the plan get ``CARRIED_STEP_SUMMARY``. Oldest step first, at most
    ``EARLIER_STEPS_MAX_ITEMS``.
    """
    if before_step_num <= 1:
        return ()
    start = current_plan_start(pivot)
    rows = (
        CasePersonStep.objects.filter(
            test_run_test_case_id=pivot.pk,
            step_num__lt=before_step_num,
            requested_at__gte=start.since,
        )
        .exclude(kind=PersonStepKind.RESTORE)
        .order_by("-requested_at", "-pk")
        .values_list("step_num", "instruction", "status", "outcome", "agent_summary")[
            :EARLIER_STEP_ROWS_MAX
        ]
    )
    newest: dict[int, AdviceEarlierStep] = {}
    for step_num, instruction, status, outcome, agent_summary in rows:
        if step_num in newest:
            continue
        newest[step_num] = AdviceEarlierStep(
            step_num=step_num,
            summary=earlier_step_summary(instruction, status, outcome, agent_summary),
        )
    for step_num in range(1, min(start.carried_count, before_step_num - 1) + 1):
        newest.setdefault(
            step_num, AdviceEarlierStep(step_num=step_num, summary=CARRIED_STEP_SUMMARY)
        )
    ordered = sorted(newest.values(), key=lambda item: item.step_num)
    return tuple(ordered[-EARLIER_STEPS_MAX_ITEMS:])


def earlier_step_summary(
    instruction: str, status: str, outcome: str, agent_summary: str
) -> str:
    """ "<instruction> — <status>[, reported: <outcome>][ — <agent summary>]"."""
    parts = [one_line(instruction, EARLIER_INSTRUCTION_MAX_CHARS)]
    result = _STATUS_LABELS.get(status, status)
    if outcome:
        result = f"{result}, reported: {_OUTCOME_LABELS.get(outcome, outcome)}"
    parts.append(result)
    if agent_summary:
        parts.append(one_line(agent_summary, EARLIER_SUMMARY_MAX_CHARS))
    return " — ".join(parts)


def one_line(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
