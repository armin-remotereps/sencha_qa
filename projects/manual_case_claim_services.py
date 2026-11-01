"""Use cases: the tester claims a Manual run case, or gives the claim back.

A claim (from the Test Runner window) puts a Created case In progress, opens
its execution segment, mints the claim token the hand-in must carry and writes
the case's verbatim steps as its checkpoint plan (``manual_case_steps``). At
most one case per run is claimed: checked under the run lock, which every
claim holds. A release puts the case back to Created and clears the claim and
the checkpoint; the window releases its own claim (with the token), a
writable member on the web releases a stuck one (no token). A claim's pause
(``manual_case_pause_services``) starts cleared and ends with it.

A claim also (re)starts the case's "How to do it" guide
(``manual_step_guide_services``; generated on the ``assist`` queue after
commit, never awaited).

Every change is broadcast on commit (``broadcast_run_and_pivots``), which also
nudges the project's Test Runner to re-read its snapshot.
"""

from __future__ import annotations

import secrets

from django.db import transaction
from django.utils import timezone

from agents.checkpoint import checkpoint_to_json
from machine_setup.turn_state import manual_case_turn_live, manual_run_turn_live
from projects.case_duration_services import (
    CASE_SEGMENT_OPEN_FIELDS,
    close_case_segment,
    open_case_segment,
)
from projects.exceptions import ManualRunError
from projects.manual_case_lock import claim_matches, lock_manual_run_case
from projects.manual_case_steps import build_manual_checkpoint
from projects.manual_run_policy import MANUAL_PAUSE_AGENT_BUSY
from projects.manual_step_guide_services import request_manual_case_guide
from projects.models import (
    MANUAL_CLAIM_CLEARED,
    MANUAL_CLAIM_FIELDS,
    MANUAL_PAUSE_CLEARED,
    Project,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.person_step_actor import PersonStepActor
from projects.services import broadcast_run_and_pivots
from test_sessions.case_activity import CaseActivity
from test_sessions.services.record_case_activity import record_case_activity

_RUN_NOT_RUNNING = "This manual run is not running."
_NOT_CLAIMABLE = "This case was already performed or is being performed."
_OTHER_CLAIMED = (
    "Another case of this run is in progress. Hand it in or release it first."
)
_NOT_CLAIMED = "This case is not claimed any more."
_SIGNED_IN_ONLY = "Only a signed-in member can release a case."
# Token bytes: 16 bytes give 22 URL-safe characters (the wire allows 32).
_CLAIM_TOKEN_BYTES = 16


def claim_manual_case(
    *, project: Project, pivot_id: int, actor: PersonStepActor
) -> str:
    """Claim a Created case of the project's running manual run; the claim token.

    Raises ``ManualRunError`` (user-safe) when the case is not one of the
    project's manual run cases, the run is not running, the case is not
    Created, another case of the run is claimed, or a turn of a manual
    discussion of the run is live. Records the actor's
    ``CaseActivity.START`` (a member only); no leaderboard credit until the
    hand-in.
    """
    with transaction.atomic():
        test_run, pivot = lock_manual_run_case(
            project_id=project.pk, pivot_id=pivot_id, with_test_case=True
        )
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
        # The agent of a case's discussion may still be on the machine.
        if manual_run_turn_live(test_run.pk):
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
        request_manual_case_guide(pivot=pivot, project_id=project.pk)
        record_case_activity(
            user=actor.user, pivot_ids=[pivot.pk], activity=CaseActivity.START
        )
        transaction.on_commit(lambda: broadcast_run_and_pivots(test_run, [pivot]))
    return token


def release_manual_case(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    claim_token: str | None,
) -> None:
    """Give the claim back: the case is Created again, its checkpoint cleared.

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
