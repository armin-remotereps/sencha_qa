"""The task text of a test case as the agents read it.

A leaf (models only), so the case worker and the Ask AI advisor word the case
the same way without importing ``projects.services``.
"""

from __future__ import annotations

import re
from typing import Final

from projects.models import TestCase
from projects.testrail_mapping import STEPS_TEMPLATE_NAME

# A numbered step as ``flatten_steps`` writes it: "<n>. " at a line start.
_STEP_MARKER: Final = re.compile(r"^(\d+)\. ", re.MULTILINE)


def build_task_description(test_case: TestCase) -> str:
    """Title, preconditions, steps and expected result as one labelled text."""
    parts: list[str] = [f"Test Case: {test_case.title}"]
    if test_case.preconditions:
        parts.append(f"\nPreconditions:\n{test_case.preconditions}")
    if test_case.steps:
        parts.append(f"\nSteps:\n{test_case.steps}")
    if test_case.expected:
        parts.append(f"\nExpected Result:\n{test_case.expected}")
    return "\n".join(parts)


def numbered_case_step_count(test_case: TestCase) -> int:
    """How many numbered steps a steps-template case has; 0 for any other case.

    Markers count only in sequence 1, 2, 3, ..., so a "2." inside step 1's
    text cannot inflate the count.
    """
    if test_case.template != STEPS_TEMPLATE_NAME:
        return 0
    count = 0
    for match in _STEP_MARKER.finditer(test_case.steps):
        if int(match.group(1)) == count + 1:
            count += 1
    return count
