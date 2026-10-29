"""The task text of a test case as the agents read it.

A leaf (models only), so the case worker and the Ask AI advisor word the case
the same way without importing ``projects.services``.
"""

from __future__ import annotations

from projects.models import TestCase


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
