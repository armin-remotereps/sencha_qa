"""The verbatim steps a Manual run tester performs, and their checkpoint plan.

Pure: no queries; works on an already loaded ``TestCase``. The steps are the
case's own text, never reworded by the AI:

- a "Test Case (Steps)" case: its numbered steps (``numbered_case_steps``);
- a free-text case: one step holding the whole steps text and the whole
  expected result (D1);
- a case with no steps: one step built from the title (the preconditions are
  shown above the steps, never inside them).

Push and evidence code should use ``projects.manual_run_policy`` (a leaf) to
recognise manual cases: this module imports ``projects.case_step_refs``, which
imports ``projects.testrail_mapping``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from agents.types import OrchestratorCheckpoint, SubTask
from controller_client.protocol import (
    DEFAULT_BROWSER,
    MANUAL_CASE_MAX_STEPS,
    MANUAL_PRECONDITIONS_MAX_CHARS,
    MANUAL_STEP_TEXT_MAX_CHARS,
)
from projects.case_step_refs import numbered_case_steps
from projects.models import PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH, TestCase

# A numbered step's text and expected result (as for person step case steps).
_NUMBERED_STEP_TEXT_MAX_CHARS: Final = PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH
# The single step of a free-text or step-less case holds the whole text.
_SINGLE_STEP_TEXT_MAX_CHARS: Final = MANUAL_STEP_TEXT_MAX_CHARS


@dataclass(frozen=True)
class ManualStep:
    """One verbatim step; ``num`` is 1-based and equals the plan position."""

    num: int
    text: str
    # Blank when the case gives no expected result for the step.
    expected: str = ""


@dataclass(frozen=True)
class ManualCaseSteps:
    # Shown above the steps; blank when the case has none.
    preconditions: str
    # 1..MANUAL_CASE_MAX_STEPS steps, numbered 1..n in order.
    steps: tuple[ManualStep, ...]
    # The case's numbered (steps-template) step count before the cap; 0 for a
    # single-step case. Stored as ``OrchestratorCheckpoint.case_step_count``.
    case_step_count: int
    # Steps beyond MANUAL_CASE_MAX_STEPS were left out.
    truncated: bool


def manual_case_steps(test_case: TestCase) -> ManualCaseSteps:
    """The case's verbatim steps (always at least one), capped."""
    preconditions = test_case.preconditions.strip()[:MANUAL_PRECONDITIONS_MAX_CHARS]
    numbered = numbered_case_steps(test_case)
    if numbered:
        return ManualCaseSteps(
            preconditions=preconditions,
            steps=tuple(
                ManualStep(
                    num=index,
                    text=step.text[:_NUMBERED_STEP_TEXT_MAX_CHARS],
                    expected=step.expected[:_NUMBERED_STEP_TEXT_MAX_CHARS],
                )
                for index, step in enumerate(numbered[:MANUAL_CASE_MAX_STEPS], start=1)
            ),
            case_step_count=len(numbered),
            truncated=len(numbered) > MANUAL_CASE_MAX_STEPS,
        )
    text = test_case.steps.strip() or test_case.title.strip()
    return ManualCaseSteps(
        preconditions=preconditions,
        steps=(
            ManualStep(
                num=1,
                text=text[:_SINGLE_STEP_TEXT_MAX_CHARS],
                expected=test_case.expected.strip()[:_SINGLE_STEP_TEXT_MAX_CHARS],
            ),
        ),
        case_step_count=0,
        truncated=False,
    )


def build_manual_checkpoint(test_case: TestCase) -> OrchestratorCheckpoint:
    """The checkpoint a claim writes: the verbatim steps as the plan.

    No attempts, cursor at step 1, the default browser, ``manual=True``. Each
    plan step of a numbered case covers its own case step (``case_steps``),
    so the Steps card and TestRail show "TestRail step N"; a single-step case
    has no references (``case_step_count`` 0).
    """
    steps = manual_case_steps(test_case)
    numbered = steps.case_step_count > 0
    return OrchestratorCheckpoint(
        plan=tuple(
            SubTask(
                description=step.text,
                expected_result=step.expected,
                timeout_seconds=0,
                case_steps=(step.num,) if numbered else (),
            )
            for step in steps.steps
        ),
        attempts=(),
        next_step=1,
        browser=DEFAULT_BROWSER,
        total_iterations=0,
        case_step_count=steps.case_step_count,
        # Meaningful only with numbered steps; the stored JSON omits it
        # otherwise, so keep the parser's default for a stable round trip.
        case_steps_testrail=bool(test_case.testrail_id) if numbered else True,
        manual=True,
    )


def manual_steps_from_checkpoint(
    checkpoint: OrchestratorCheckpoint,
) -> tuple[ManualStep, ...]:
    """The verbatim steps stored by the claim, as the window shows them."""
    return tuple(
        ManualStep(num=index, text=task.description, expected=task.expected_result)
        for index, task in enumerate(checkpoint.plan, start=1)
    )
