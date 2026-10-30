"""Labels for the test case steps a plan step covers.

Pure: no ORM. One formatter for the Test Runner window, the "Your turn" card,
the Steps card and the chat briefs.
"""

from __future__ import annotations

from collections.abc import Sequence

from agents.case_step_refs import lenient_case_step_refs


def format_case_steps_label(refs: Sequence[int]) -> str:
    """``""``, "TestRail step 3", "TestRail steps 3–5" or "TestRail steps 1, 3–4"."""
    if not refs:
        return ""
    numbers = sorted(set(refs))
    noun = "step" if len(numbers) == 1 else "steps"
    return f"TestRail {noun} {_ranges(numbers)}"


def stored_case_steps(value: object) -> tuple[int, ...]:
    """A ``CasePersonStep.case_steps`` JSON value, read leniently."""
    return lenient_case_step_refs(value)


def _ranges(numbers: Sequence[int]) -> str:
    parts: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number != previous + 1:
            parts.append(_range(start, previous))
            start = number
        previous = number
    parts.append(_range(start, previous))
    return ", ".join(parts)


def _range(start: int, end: int) -> str:
    if start == end:
        return str(start)
    # Two neighbours read better as a list than as a range.
    if end == start + 1:
        return f"{start}, {end}"
    return f"{start}–{end}"
