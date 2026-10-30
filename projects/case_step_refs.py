"""The numbered test case steps a plan step covers: texts and labels.

No queries: everything works on an already loaded ``TestCase`` or on stored
JSON. One formatter for the Test Runner window, the "Your turn" card, the
Steps card, the Ask AI context and the chat briefs.

Only "Test Case (Steps)" cases have reliable numbers: ``flatten_steps`` writes
their steps (and the expected results of the steps that have one) as
"<n>. <text>" blocks separated by a blank line. Free-text cases have none.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from agents.case_step_refs import lenient_case_step_refs
from projects.case_step_labels import format_case_steps_label
from projects.models import (
    PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH,
    PERSON_STEP_CASE_STEPS_MAX_ITEMS,
    TestCase,
)
from projects.testrail_mapping import STEPS_TEMPLATE_NAME

# A numbered step as ``flatten_steps`` writes it: "<n>. " at the start of the
# text or after a blank line (steps are joined with one), so a numbered list
# inside a step's own text rarely starts a new step.
_STEP_MARKER: Final = re.compile(r"(?:\A|\n\n)(\d+)\. ")


@dataclass(frozen=True)
class CaseStepText:
    num: int
    text: str
    # Blank when the case gives no expected result for the step.
    expected: str = ""


def numbered_case_steps(test_case: TestCase) -> tuple[CaseStepText, ...]:
    """Every numbered step of a steps-template case; ``()`` for any other case.

    Step markers count only in sequence 1, 2, 3, ..., so a "2." inside step
    1's text cannot inflate the count or split the step.
    """
    if test_case.template != STEPS_TEMPLATE_NAME:
        return ()
    steps = _sections(test_case.steps, sequential=True, limit=None)
    expected = dict(_sections(test_case.expected, sequential=False, limit=len(steps)))
    return tuple(
        CaseStepText(num=num, text=text, expected=expected.get(num, ""))
        for num, text in steps
    )


def case_steps_label(test_case: TestCase, refs: Sequence[int]) -> str:
    return format_case_steps_label(refs, testrail=bool(test_case.testrail_id))


def case_step_rows(test_case: TestCase, refs: Sequence[int]) -> list[dict[str, object]]:
    """The covered steps as stored on ``CasePersonStep.case_steps`` (capped)."""
    if not refs:
        return []
    wanted = set(refs)
    return [
        {
            "num": step.num,
            "text": step.text[:PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH],
            "expected": step.expected[:PERSON_STEP_CASE_STEP_TEXT_MAX_LENGTH],
        }
        for step in numbered_case_steps(test_case)
        if step.num in wanted
    ][:PERSON_STEP_CASE_STEPS_MAX_ITEMS]


def stored_case_steps(value: object) -> tuple[CaseStepText, ...]:
    """A ``CasePersonStep.case_steps`` JSON value, read leniently."""
    if not isinstance(value, list):
        return ()
    steps: list[CaseStepText] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        num, text, expected = item.get("num"), item.get("text"), item.get("expected")
        if not lenient_case_step_refs([num]) or not isinstance(text, str):
            continue
        steps.append(
            CaseStepText(
                num=cast(int, num),
                text=text,
                expected=expected if isinstance(expected, str) else "",
            )
        )
    return tuple(steps[:PERSON_STEP_CASE_STEPS_MAX_ITEMS])


def _sections(
    text: str, *, sequential: bool, limit: int | None
) -> list[tuple[int, str]]:
    """``(n, text)`` blocks of "<n>. " markers.

    ``sequential``: only n = previous + 1 starts a block (steps). Otherwise any
    n above the previous one and within ``limit`` does (expected results are
    numbered by step and skip steps without one).
    """
    starts: list[tuple[int, int, int]] = []
    previous = 0
    for match in _STEP_MARKER.finditer(text):
        num = int(match.group(1))
        if sequential and num != previous + 1:
            continue
        if not sequential and (num <= previous or (limit is not None and num > limit)):
            continue
        starts.append((num, match.start(1), match.end()))
        previous = num
    sections: list[tuple[int, str]] = []
    for index, (num, _, body_start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(text)
        sections.append((num, text[body_start:end].strip()))
    return sections
