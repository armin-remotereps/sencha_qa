"""Use cases: the tester claims a Manual run case, or gives the claim back.

A claim (from the Test Runner window) puts a Created case In progress, opens
its execution segment, mints the claim token the hand-in must carry and writes
the case's verbatim steps as its checkpoint plan (``manual_case_steps``). At
most one case per run is claimed: checked under the run lock, which every
claim holds. A release puts the case back to Created and clears the claim and
the checkpoint; the window releases its own claim (with the token), a
writable member on the web releases a stuck one (no token). A claim's pause
(``manual_case_pause_services``) starts cleared and ends with it.

Runner (Q12): a Manual run's Test Runner is optional. When the run has one,
a claim holds it (``projects.runner_use``), so the claim locks the runner row
first (lock order runner, project, run) and is refused while anything else
holds it. A claim from a runner window (``runner_id``) is only for a run
assigned to that runner.

Every change is broadcast on commit (``broadcast_run_and_pivots``), which also
nudges the run's Test Runner to re-read its snapshot.
"""

from __future__ import annotations

import secrets

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_to_json
from machine_setup.turn_state import live_turn_exists, manual_case_turn_live
from projects.case_duration_services import (
    CASE_SEGMENT_OPEN_FIELDS,
    close_case_segment,
    open_case_segment,
)
from projects.exceptions import ManualRunError
from projects.manual_case_lock import claim_matches, lock_manual_run_case
from projects.manual_case_steps import build_manual_checkpoint
from projects.manual_run_policy import MANUAL_PAUSE_AGENT_BUSY
from projects.models import (
    MANUAL_CLAIM_CLEARED,
    MANUAL_CLAIM_FIELDS,
    MANUAL_PAUSE_CLEARED,
    ManualClaimSource,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.runner_use import runner_hold
from projects.services import broadcast_run_and_pivots
from runners.locks import lock_runner_row
from runners.services.runner_access import runner_serves_project
from runners.types import RunnerId
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_RUN_NOT_RUNNING = "This manual run is not running."
_NOT_CLAIMABLE = "This case was already performed or is being performed."
_OTHER_CLAIMED = (
    "Another case of this run is in progress. Hand it in or release it first."
)
_NOT_CLAIMED = "This case is not claimed any more."
_SIGNED_IN_ONLY = "Only a signed-in member can release a case."
_NOT_THIS_RUNNER = "This manual run is not assigned to this Test Runner."
RUNNER_CHANGED_MESSAGE = "The run's Test Runner changed. Look at the list again."
# Neutral (also an MCP refusal): names neither the runner's owner nor why.
RUN_MACHINE_UNAVAILABLE = (
    "The run's test machine is no longer available. Ask a project member to "
    "choose another one."
)
# Token bytes: 16 bytes give 22 URL-safe characters (the wire allows 32).
_CLAIM_TOKEN_BYTES = 16


def claim_manual_case(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    source: ManualClaimSource = "",
    runner_id: RunnerId | None = None,
) -> str:
    """Claim a Created case of one of the project's running manual runs; the
    claim token.

    Raises ``ManualRunError`` (user-safe) when the case is not one of the
    project's manual run cases, the run is not running, the case is not
    Created, another case of the run is claimed, the run's Test Runner is
    held by anything else, or any Machine setup turn of the project is live
    (it may be using the machine). ``runner_id``: the claim comes from that
    runner's window, so the run must be assigned to it. ``source`` is the
    internal ``manual_claim_source`` (blank, or ``MANUAL_SOURCE_MCP``).
    Records the actor's ``CaseActivity.START`` (a member only); no
    leaderboard credit until the hand-in.
    """
    with transaction.atomic():
        run_runner_id = lock_free_case_runner(project.pk, pivot_id)
        if runner_id is not None and run_runner_id != runner_id:
            raise ManualRunError(_NOT_THIS_RUNNER)
        test_run, pivot = lock_manual_run_case(
            project_id=project.pk, pivot_id=pivot_id, with_test_case=True
        )
        if test_run.runner_id != run_runner_id:
            raise ManualRunError(RUNNER_CHANGED_MESSAGE)
        if test_run.status != TestRunStatus.STARTED:
            raise ManualRunError(_RUN_NOT_RUNNING)
        if pivot.status != TestRunTestCaseStatus.CREATED:
            raise ManualRunError(_NOT_CLAIMABLE)
        # Under the run lock, which every claim takes: one claimed case per run.
        if (
            TestRunTestCase.objects.filter(test_run_id=test_run.pk)
            .exclude(manual_claim_token="")
            .exclude(pk=pivot.pk)
            .exists()
        ):
            raise ManualRunError(_OTHER_CLAIMED)
        # A setup agent (any thread) may be on the machine; the project row
        # lock serializes this with a setup send.
        if live_turn_exists(project.pk, timezone.now()):
            raise ManualRunError(MANUAL_PAUSE_AGENT_BUSY)
        now = timezone.now()
        token = secrets.token_urlsafe(_CLAIM_TOKEN_BYTES)
        pivot.status = TestRunTestCaseStatus.IN_PROGRESS
        pivot.result = ""
        pivot.blocked_reason = ""
        pivot.failed_by = ""
        pivot.finished_at = None
        open_case_segment(pivot, now)
        pivot.manual_claim_token = token
        pivot.manual_claim_source = source
        pivot.manual_claimed_by = actor.user
        pivot.manual_claimed_label = actor.label()
        pivot.manual_claimed_at = now
        # A new claim starts unpaused (saved with ``MANUAL_CLAIM_FIELDS``).
        for field, value in MANUAL_PAUSE_CLEARED.items():
            setattr(pivot, field, value)
        pivot.execution_checkpoint = checkpoint_to_json(
            build_manual_checkpoint(pivot.test_case)
        )
        pivot.save(
            update_fields=[
                "status",
                "result",
                "blocked_reason",
                "failed_by",
                "finished_at",
                *CASE_SEGMENT_OPEN_FIELDS,
                *MANUAL_CLAIM_FIELDS,
                "execution_checkpoint",
                "updated_at",
            ]
        )
        record_case_activity(
            user=actor.user, pivot_ids=[pivot.pk], activity=CaseActivity.START
        )
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return token


def lock_free_case_runner(project_id: int, pivot_id: int) -> RunnerId | None:
    """Lock the runner of the case's run (before project and run) and refuse
    while anything else holds it; returns the runner id (None: no runner).

    For the paths that make a Manual case hold its runner: claiming it and
    continuing it after a pause. Callers re-check ``test_run.runner_id``
    against the result under the run lock (``RUNNER_CHANGED_MESSAGE``).
    Must be called inside ``transaction.atomic()``.
    """
    run_runner_id = _run_runner_id(project_id, pivot_id)
    if run_runner_id is not None:
        _lock_free_runner(run_runner_id, project_id)
    return run_runner_id


def _run_runner_id(project_id: int, pivot_id: int) -> RunnerId | None:
    """The runner of the case's run, read before any lock (one query); the
    caller re-checks it under the run lock."""
    raw = (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project_id=project_id)
        .values_list("test_run__runner_id", flat=True)
        .first()
    )
    return RunnerId(raw) if raw is not None else None


def _lock_free_runner(runner_id: RunnerId, project_id: int) -> None:
    """Lock the runner row (before the project and run) and refuse while it
    no longer serves the project (archived, its owner deactivated or no
    longer a writable member) or anything else holds it. Connection is not
    required: a claim through MCP may proceed while the runner is offline
    (its machine tools then answer that the test machine is offline)."""
    runner = lock_runner_row(runner_id, fields=("id", "owner_id", "archived_at"))
    if runner is None or not runner_serves_project(runner, project_id):
        raise ManualRunError(RUN_MACHINE_UNAVAILABLE)
    hold = runner_hold(runner_id)
    if hold is not None:
        raise ManualRunError(hold.message)


def release_manual_case(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    claim_token: str | None,
) -> bool:
    """Give the claim back: the case is Created again, its checkpoint cleared.

    Returns whether the released claim was made at the Test Runner window
    (``manual_claim_at_runner``), so the caller can word its message.

    ``claim_token``: the claim the Test Runner window gives back; ``None`` is a
    writable member's force release on the web (the view authorizes it; a
    signed-in ``actor`` is required). The case's earlier time stays in
    ``active_duration`` (a paused case's segment is already closed; closing
    again adds nothing), and its pause clears with the claim. Raises ``ManualRunError`` (user-safe) when the case
    is not claimed (or by another claim), or while a turn of its manual
    discussion is live. Allowed whatever the run's status,
    so a stuck claim can always be cleared.
    """
    if claim_token is None and actor.user is None:
        raise ManualRunError(_SIGNED_IN_ONLY)
    with transaction.atomic():
        test_run, pivot = lock_manual_run_case(project_id=project.pk, pivot_id=pivot_id)
        if pivot.status != TestRunTestCaseStatus.IN_PROGRESS or (
            not pivot.manual_claim_token
        ):
            raise ManualRunError(_NOT_CLAIMED)
        if claim_token is not None and not claim_matches(pivot, claim_token):
            raise ManualRunError(_NOT_CLAIMED)
        # Not while the discussion's agent may be using the machine.
        if manual_case_turn_live(pivot.pk):
            raise ManualRunError(MANUAL_PAUSE_AGENT_BUSY)
        at_runner = pivot.manual_claim_at_runner
        now = timezone.now()
        close_case_segment(pivot.pk, now)
        pivot.segment_started_at = None
        pivot.status = TestRunTestCaseStatus.CREATED
        pivot.execution_checkpoint = {}
        for field, value in MANUAL_CLAIM_CLEARED.items():
            setattr(pivot, field, value)
        # The segment fields were written by ``close_case_segment``.
        pivot.save(
            update_fields=[
                "status",
                "execution_checkpoint",
                *MANUAL_CLAIM_FIELDS,
                "updated_at",
            ]
        )
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return at_runner
