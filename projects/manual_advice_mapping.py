"""Manual run steps as the Ask AI advisor's step (D12).

The Manual mapper onto ``projects.advice_context.build_advice_context``, next
to the Human in the loop one (``person_step_advice_mapping``): the claimed
case's verbatim step (from its checkpoint plan, written by the claim) becomes
an ``AdviceStep(source="case")``, with the step's "How to do it" when the
caller loaded one. There are no earlier steps: the tester's marks stay in the
Test Runner window until the hand-in. No queries.
"""

from __future__ import annotations

from typing import Final

from agents.checkpoint import checkpoint_from_json
from agents.types import AdviceStep, PersonHowToItem
from projects.manual_case_steps import ManualStep, manual_steps_from_checkpoint
from projects.models import TestRunTestCase

# ``step_num`` of the advisor's step when the tester asks about the whole case.
WHOLE_CASE_STEP_NUM: Final = 0
_WHOLE_CASE_LABEL: Final = "The whole case"
_WHOLE_CASE_INSTRUCTION: Final = (
    "The person asks about the test case as a whole, not one step of it."
)


def manual_advice_step(
    pivot: TestRunTestCase,
    step_num: int | None,
    *,
    how_to: tuple[PersonHowToItem, ...] = (),
) -> AdviceStep:
    """The claimed case's step ``step_num`` (None: the whole case) for the advisor.

    ``pivot.execution_checkpoint`` and ``pivot.test_case`` must be loaded. A
    step the checkpoint does not have (released or re-run meanwhile) falls back
    to the whole case, which never carries ``how_to`` (the step's guide items).
    """
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    steps = manual_steps_from_checkpoint(checkpoint) if checkpoint is not None else ()
    step = (
        steps[step_num - 1]
        if step_num is not None and 1 <= step_num <= len(steps)
        else None
    )
    if step is None:
        return AdviceStep(
            source="case",
            kind="case",
            step_num=WHOLE_CASE_STEP_NUM,
            label=_WHOLE_CASE_LABEL,
            instruction=_WHOLE_CASE_INSTRUCTION,
        )
    return AdviceStep(
        source="case",
        kind="case",
        step_num=step.num,
        label=_step_label(step, len(steps), testrail=bool(pivot.test_case.testrail_id)),
        instruction=step.text,
        # The step is the case's own text: no separate covered case steps.
        expected_result=step.expected,
        how_to=how_to,
    )


def _step_label(step: ManualStep, total: int, *, testrail: bool) -> str:
    """ "TestRail step 3 of 6" (a TestRail case) or "Case step 3 of 6"."""
    prefix = "TestRail step" if testrail else "Case step"
    return f"{prefix} {step.num} of {total}"
