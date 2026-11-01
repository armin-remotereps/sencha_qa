"""A test case's text as people read it: TestRail markup turned into plain text.

No queries: works on an already loaded ``TestCase`` (or stored step rows). The
Test Runner window and the web case page show this; editing, agent prompts and
the TestRail push keep the raw fields.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from projects.case_markup import markup_to_plain
from projects.case_step_refs import CaseStepText, numbered_case_steps
from projects.models import TestCase


@dataclass(frozen=True)
class CaseTextDisplay:
    preconditions: str
    steps_text: str
    expected_text: str
    # Set for "Test Case (Steps)" cases only; each step converted on its own.
    numbered_steps: tuple[CaseStepText, ...]
    # The numbered steps hold the whole steps field (no text before "1."), so
    # a page may list them instead of ``steps_text``.
    steps_listed: bool
    # Likewise for the expected field: the listed steps carry all of it, so a
    # page listing the steps need not show ``expected_text`` again.
    steps_cover_expected: bool


def case_text_display(test_case: TestCase) -> CaseTextDisplay:
    """The case's fields without HTML tags or Markdown markers.

    Numbered steps are converted after splitting: converting the flattened
    field whole would let the HTML pass merge the blank lines between steps.
    The whole fields are converted too, so no text outside the numbered
    sections is lost.
    """
    numbered = case_steps_for_display(numbered_case_steps(test_case))
    # The parser takes a first section only at the very start of the field, so
    # the field starting with that exact marker means no text comes before it.
    steps_listed = bool(numbered) and test_case.steps.startswith(f"{numbered[0].num}. ")
    first_expected = next((step.num for step in numbered if step.expected), None)
    return CaseTextDisplay(
        preconditions=markup_to_plain(test_case.preconditions),
        steps_text=markup_to_plain(test_case.steps),
        expected_text=markup_to_plain(test_case.expected),
        numbered_steps=numbered,
        steps_listed=steps_listed,
        steps_cover_expected=(
            steps_listed
            and first_expected is not None
            and test_case.expected.startswith(f"{first_expected}. ")
        ),
    )


def case_steps_for_display(
    steps: Sequence[CaseStepText],
) -> tuple[CaseStepText, ...]:
    return tuple(
        CaseStepText(
            num=step.num,
            text=markup_to_plain(step.text),
            expected=markup_to_plain(step.expected),
        )
        for step in steps
    )
