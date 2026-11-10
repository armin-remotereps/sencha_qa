"""The Test Runner row lock: the one way every holder of a runner takes it.

Lock order of every acquiring service: runner row(s) (by id when several),
then project (while the Phase 1 per-project rules live), then run. A leaf:
imports only the runner model and types, so every app may use it.
"""

from __future__ import annotations

from collections.abc import Sequence

from runners.models import TestRunner
from runners.types import RunnerId


def lock_runner_row(
    runner_id: RunnerId | None, *, fields: Sequence[str] = ()
) -> TestRunner | None:
    """The runner row under ``select_for_update`` (call inside
    ``transaction.atomic``); None for no runner or a deleted one.

    ``fields``: load only these columns (empty: every column). Archived rows
    are locked too; callers decide what an archived runner means.
    """
    if runner_id is None:
        return None
    rows = TestRunner.objects.select_for_update().filter(pk=runner_id)
    if fields:
        rows = rows.only(*fields)
    return rows.first()
