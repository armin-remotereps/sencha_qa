"""Labels for the numbered test case steps a plan step covers.

A leaf (no project imports), so the Steps card and the person step pages
share one formatter without an import cycle.
"""

from __future__ import annotations

from collections.abc import Sequence


def format_case_steps_label(refs: Sequence[int], *, testrail: bool) -> str:
    """``""``, "TestRail step 3", "TestRail steps 3–5" or "Case steps 1, 3–5".

    "Case" for a case not imported from TestRail.
    """
    if not refs:
        return ""
    numbers = sorted(set(refs))
    source = "TestRail" if testrail else "Case"
    noun = "step" if len(numbers) == 1 else "steps"
    return f"{source} {noun} {_ranges(numbers)}"


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
