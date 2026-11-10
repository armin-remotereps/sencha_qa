"""A run switched Test Runner: its queued cases must not go on from the old
machine's screen (plan Q6).

A leaf (checkpoint helpers and models only) so ``run_runner_assignment``
calls it under the switch's locks without importing the person step or help
services.
"""

from __future__ import annotations

import dataclasses
from typing import Final

from django.db.models import Q
from django.utils import timezone

from agents.checkpoint import (
    clear_help_continue_pending,
    read_checkpoint_person_step,
    with_person_step,
)
from projects.models import (
    OPEN_PERSON_STEP_STATUSES,
    CasePersonStep,
    PersonStepStatus,
    TestRun,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

# ``person`` key states the worker would settle from the current screen.
_SCREEN_CONTINUATION_STATES: Final[frozenset[str]] = frozenset(
    {"reported", "overruled", "handed_back"}
)


def drop_screen_continuations(test_run: TestRun) -> list[TestRunTestCase]:
    """Make every queued (Created) case of the run restore instead of going
    on from the current screen; returns the changed cases.

    Call under the switch's locks (runners, project, run). A ``person`` key
    that would continue from the current screen (a report, an overrule or a
    hand-back still pending: queueing failed earlier) becomes a hand-back
    without ``continue_pending`` (the agent runs the step itself after
    cleanup and restore; its open row closes as handed back), and a
    ``help`` key's ``continue_pending`` is cleared. One query for the cases
    that have such a key, one save per changed case, one update for the rows.
    """
    changed: list[TestRunTestCase] = []
    closed_ids: list[int] = []
    pivots = (
        TestRunTestCase.objects.filter(
            Q(execution_checkpoint__has_key="person")
            | Q(execution_checkpoint__has_key="help"),
            test_run_id=test_run.pk,
            status=TestRunTestCaseStatus.CREATED,
        )
        .select_for_update()
        .only("id", "execution_checkpoint")
    )
    for pivot in pivots:
        original = pivot.execution_checkpoint
        checkpoint: object = clear_help_continue_pending(original)
        person = read_checkpoint_person_step(checkpoint)
        if (
            person is not None
            and person.continue_pending
            and person.state in _SCREEN_CONTINUATION_STATES
        ):
            checkpoint = with_person_step(
                checkpoint,
                dataclasses.replace(
                    person, state="handed_back", continue_pending=False
                ),
            )
            if person.request_id is not None:
                closed_ids.append(person.request_id)
        if checkpoint != original:
            pivot.execution_checkpoint = checkpoint
            pivot.save(update_fields=["execution_checkpoint", "updated_at"])
            changed.append(pivot)
    if closed_ids:
        CasePersonStep.objects.filter(
            pk__in=closed_ids, status__in=OPEN_PERSON_STEP_STATUSES
        ).update(status=PersonStepStatus.HANDED_BACK, closed_at=timezone.now())
    return changed
