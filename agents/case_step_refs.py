"""Validation of the case step numbers a plan step covers.

Pure: no ORM. A plan step may name the test case's numbered steps (1-based) it
performs or checks; anything malformed or out of range is dropped silently, so
a bad reference never fails a plan, a checkpoint or a redesign.
"""

from __future__ import annotations

from typing import cast

# A plan step never covers more; keeps labels and stored JSON small.
MAX_CASE_STEP_REFS = 50


def normalize_case_step_refs(raw: object, step_count: int) -> tuple[int, ...]:
    """Sorted, unique ints in ``1..step_count``; ``()`` for anything else."""
    if step_count <= 0:
        return ()
    return _refs(raw, step_count)


def lenient_case_step_refs(raw: object) -> tuple[int, ...]:
    """Stored refs whose bound is unknown: positive ints only, sorted, capped."""
    return _refs(raw, None)


def _refs(raw: object, step_count: int | None) -> tuple[int, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    refs = {
        value
        for value in cast(list[object], list(raw))
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 1
        and (step_count is None or value <= step_count)
    }
    return tuple(sorted(refs)[:MAX_CASE_STEP_REFS])
