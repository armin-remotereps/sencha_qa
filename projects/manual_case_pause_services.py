"""Use cases: the tester pauses the claimed Manual run case, and continues it.

A manual pause is the tester's break, not the agent pause
(``case_pause_services``, which stops a worker and opens a Need help
request): the case stays In progress, the run stays Started, and the claim,
its token and the marks in the runner's spool are untouched. Pausing closes
the case's execution segment, so the paused time never reaches
``active_duration`` (nor TestRail elapsed); continuing opens a new one.

The Test Runner window pauses its own claim (with the token); a writable
member pauses or continues from the web (no token; the view authorizes it).
Continue is refused while any Machine setup turn of the project is live: a
setup agent may be using the machine (``machine_setup.turn_state``, a leaf).

The pause fields (``MANUAL_PAUSE_FIELDS``) belong to the claim: every path
that ends a claim (release, hand-in, Stop run, Needs re-test, Re-run, Reset)
clears them through ``MANUAL_CLAIM_CLEARED``. No ntfy, credit or test-session
activity. Every change is broadcast on commit (``broadcast_run_and_pivots``),
which also nudges the project's Test Runner to re-read its snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import transaction
from django.utils import timezone

from machine_setup.turn_state import live_turn_exists
from projects.case_duration_services import (
    CASE_SEGMENT_OPEN_FIELDS,
    close_case_segment,
    open_case_segment,
)
from projects.exceptions import ManualRunError
from projects.manual_case_claim_services import (
    RUNNER_CHANGED_MESSAGE,
    lock_free_case_runner,
)
from projects.manual_case_lock import NOT_MANUAL, claim_matches, lock_manual_run_case
from projects.manual_run_policy import (
    MANUAL_PAUSE_AGENT_BUSY,
    MANUAL_PAUSE_NOT_CLAIMED,
    MANUAL_PAUSE_NOT_PAUSED,
    MANUAL_PAUSE_STALE_CLAIM,
    MANUAL_RUN_NOT_RUNNING,
    is_claimed_manual_case,
    is_manual_run,
)
from projects.models import (
    MANUAL_PAUSE_CLEARED,
    MANUAL_PAUSE_FIELDS,
    Project,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
)
from projects.person_step_actor import PersonStepActor
from projects.services import broadcast_run_and_pivots

_SIGNED_IN_ONLY: Final = "Only a signed-in member can pause or continue a case."


@dataclass(frozen=True)
class ManualPausePanel:
    """Pause / Continue of a manual case, for the case page, run page and the
    runner snapshot (built from loaded fields, no query)."""

    paused: bool
    # "Ana via Test Runner"; "" when not paused.
    paused_label: str
    paused_at: datetime | None
    can_pause: bool
    can_continue: bool
    # User-safe; "" when an action applies.
    blocker: str


def get_manual_pause_panel(
    test_run: TestRun, pivot: TestRunTestCase
) -> ManualPausePanel:
    """The case's pause panel from loaded fields (no query).

    ``test_run`` needs ``status`` and ``help_mode``; ``pivot`` its status,
    claim token and source, and pause fields. Whether the discussion agent is busy is the
    Continue service's check, not the panel's.
    """
    paused = pivot.manual_paused_at is not None
    blocker = ""
    if not is_manual_run(test_run):
        blocker = NOT_MANUAL
    elif test_run.status != TestRunStatus.STARTED:
        blocker = MANUAL_RUN_NOT_RUNNING
    elif not is_claimed_manual_case(test_run, pivot):
        blocker = MANUAL_PAUSE_NOT_CLAIMED
    # A claim made outside the Test Runner window has no Pause / Continue
    # (and no hint: the source is never shown).
    actionable = not blocker and pivot.manual_claim_at_runner
    return ManualPausePanel(
        paused=paused,
        paused_label=pivot.manual_paused_label if paused else "",
        paused_at=pivot.manual_paused_at,
        can_pause=actionable and not paused,
        can_continue=actionable and paused,
        blocker=blocker,
    )


def pause_manual_case(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    claim_token: str | None,
) -> bool:
    """Pause the claimed case; False when it was already paused (no change).

    ``claim_token``: the claim the Test Runner window holds; ``None`` is a
    writable member on the web (a signed-in ``actor`` is required). Raises
    ``ManualRunError`` (user-safe).
    """
    _check_web_actor(actor, claim_token)
    with transaction.atomic():
        test_run, pivot = _lock_claimed_case(
            project=project, pivot_id=pivot_id, claim_token=claim_token
        )
        if pivot.manual_paused_at is not None:
            return False
        now = timezone.now()
        # One conditional UPDATE; the in-memory duration is not saved below.
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
        pivot.manual_paused_at = now
        pivot.manual_paused_by = actor.user
        pivot.manual_paused_label = actor.label()
        pivot.manual_paused_via = actor.via
        pivot.save(update_fields=[*MANUAL_PAUSE_FIELDS, "updated_at"])
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return True


def continue_manual_case(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    claim_token: str | None,
) -> None:
    """Continue the paused claimed case: its clock runs again.

    Same callers as ``pause_manual_case``. Raises ``ManualRunError``
    (user-safe) when the case is not paused, while any Machine setup turn of
    the project is live, or while something else holds the run's Test Runner.
    """
    _check_web_actor(actor, claim_token)
    with transaction.atomic():
        # Continuing makes the case hold its runner again: refuse while
        # anything else holds it (runner row first, then project and run).
        run_runner_id = lock_free_case_runner(project.pk, pivot_id)
        test_run, pivot = _lock_claimed_case(
            project=project, pivot_id=pivot_id, claim_token=claim_token
        )
        if test_run.runner_id != run_runner_id:
            raise ManualRunError(RUNNER_CHANGED_MESSAGE)
        if pivot.manual_paused_at is None:
            raise ManualRunError(MANUAL_PAUSE_NOT_PAUSED)
        now = timezone.now()
        if live_turn_exists(project.pk, now):
            raise ManualRunError(MANUAL_PAUSE_AGENT_BUSY)
        open_case_segment(pivot, now)
        for field, value in MANUAL_PAUSE_CLEARED.items():
            setattr(pivot, field, value)
        pivot.save(
            update_fields=[
                *CASE_SEGMENT_OPEN_FIELDS,
                *MANUAL_PAUSE_FIELDS,
                "updated_at",
            ]
        )
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))


def _check_web_actor(actor: PersonStepActor, claim_token: str | None) -> None:
    if claim_token is None and actor.user is None:
        raise ManualRunError(_SIGNED_IN_ONLY)


def _lock_claimed_case(
    *, project: Project, pivot_id: int, claim_token: str | None
) -> tuple[TestRun, TestRunTestCase]:
    """Lock project, run and case; refuse unless the case is the claimed case
    of the running manual run (and, from the runner, holds ``claim_token``).

    Must be called inside ``transaction.atomic()``.
    """
    test_run, pivot = lock_manual_run_case(project_id=project.pk, pivot_id=pivot_id)
    if test_run.status != TestRunStatus.STARTED:
        raise ManualRunError(MANUAL_RUN_NOT_RUNNING)
    claimed = is_claimed_manual_case(test_run, pivot)
    if claim_token is not None:
        if not claimed or not claim_matches(pivot, claim_token):
            raise ManualRunError(MANUAL_PAUSE_STALE_CLAIM)
    elif not claimed or not pivot.manual_claim_at_runner:
        # A claim made outside the Test Runner window is never paused from
        # the web; same neutral refusal as an unclaimed case.
        raise ManualRunError(MANUAL_PAUSE_NOT_CLAIMED)
    return test_run, pivot
