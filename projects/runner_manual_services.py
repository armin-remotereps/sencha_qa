"""A Test Runner acts on a Manual run assigned to it (server-side seam).

The runner window claims and releases a case, hands it in, uploads its
screenshots (``MANUAL_RUN_V1``); it also pauses
and continues the claimed case and reports the tester's marks so far
(``MANUAL_PAUSE_DISCUSS_V1``; the consumer checks the capability). Every rule
of those actions stays in the manual services (``manual_case_claim_services``,
``manual_case_pause_services``, ``manual_case_progress_services``,
``manual_case_submit_services``, ``manual_screenshot_services``). This module
applies the rules of ``runner_person_step_services.answer_from_runner``: only
the connection that owns the runner, a case of a Manual run assigned to the
runner (``TestRun.runner``), a throttle, and the person is the runner's owner
("<owner> via Test Runner") while a writable member of the run's project
(else refused).

Hand-ins record the runner that handed them in (``ManualCaseSubmission.runner``)
and their screenshots are accepted only from it.

Throttles (per runner): claim, release, pause, continue and hand-in share the
runner answer throttle (20/min, ``check_runner_answer_throttle``); progress
reports have their own (``_PROGRESS_LIMIT`` per minute, so they never starve
the others); screenshot uploads have their own budget
(``MANUAL_SHOT_RATE_PER_MINUTE``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from django.conf import settings
from django.db import transaction

from controller_client.exceptions import ProtocolError
from controller_client.protocol import (
    ManualCaseActionKind,
    ManualCaseProgressPayload,
    ManualCaseSubmitPayload,
    parse_manual_screenshot_upload_payload,
    peek_manual_upload_ids,
)
from projects.exceptions import (
    ManualRunError,
    ManualShotRejectedError,
    ManualSubmissionDroppedError,
    PersonStepError,
)
from projects.manual_case_claim_services import (
    claim_manual_case,
    release_manual_case,
)
from projects.manual_case_pause_services import (
    continue_manual_case,
    pause_manual_case,
)
from projects.manual_case_progress_services import record_manual_progress
from projects.manual_case_submit_services import submit_manual_case
from projects.manual_screenshot_services import (
    reject_manual_shot,
    store_manual_screenshot,
)
from projects.models import ManualCaseSubmission, Project, ScreenshotCaptureSource
from projects.person_step_actor import PersonStepActor

# The shared helpers of the runner's answers; the answer throttle key and
# limit must stay the same for every runner answer.
from projects.runner_person_step_services import (
    NOT_RUNNER_MEMBER,
    check_runner_answer_throttle,
    hit_runner_throttle,
    owns_runner,
    runner_actor,
)
from runners.types import RunnerId

_NOT_OWNER = "This Test Runner connection was replaced or closed. Reconnect it."
_NOT_ASSIGNED = "This manual run is not assigned to this Test Runner."
_UNKNOWN_HAND_IN = "This hand-in is not known for this Test Runner any more."
_UPLOAD_THROTTLED = "Too many screenshots at once. The upload retries shortly."
_UPLOAD_WINDOW_SECONDS = 60
_CLAIMED = "Claimed. The case is yours."
_RELEASED = "Released. The case is back in the list."
_PAUSED = "Paused. The clock is stopped until you continue testing."
_ALREADY_PAUSED = "The case was already paused."
_CONTINUED = "Go on testing. The clock runs again."
_PROGRESS_THROTTLED = "Too many progress reports. The next one is sent shortly."
# Progress reports per runner and minute (the window debounces them to one
# every two seconds at most while the tester marks steps, plus a flush before
# each pause or discussion message, so twice that rate leaves headroom).
_PROGRESS_LIMIT = 60
_PROGRESS_WINDOW_SECONDS = 60


def _check_owner_and_throttle(runner_id: RunnerId, channel_name: str) -> None:
    if not owns_runner(runner_id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    try:
        check_runner_answer_throttle(runner_id)
    except PersonStepError as exc:
        raise ManualRunError(exc.message) from exc


def runner_manual_case_project(runner_id: RunnerId, pivot_id: int) -> Project:
    """The project of a case of a run assigned to the runner (one query).

    Also used by the consumer before a Manual discussion send
    (``machine_setup.services.runner_discussion.send_manual_from_runner``).

    Raises ``ManualRunError`` when the case is not one of the runner's runs;
    the manual services check the run's mode and state under their locks.
    """
    project = Project.objects.filter(
        test_runs__runner_id=runner_id, test_runs__pivot_entries__pk=pivot_id
    ).first()
    if project is None:
        raise ManualRunError(_NOT_ASSIGNED)
    return project


def _actor(runner_id: RunnerId, project: Project) -> PersonStepActor:
    actor = runner_actor(runner_id, project.pk)
    if actor is None:
        raise ManualRunError(NOT_RUNNER_MEMBER)
    return actor


def act_from_runner(
    *,
    runner_id: RunnerId,
    channel_name: str,
    action: ManualCaseActionKind,
    pivot_id: int,
    claim_token: str,
) -> str:
    """Claim, release, pause or continue a case of a Manual run assigned to
    the runner; the window's text.

    ``claim_token`` is the claim a release, pause or continue acts on (blank
    for a claim). Raises ``ManualRunError`` (user-safe).
    """
    _check_owner_and_throttle(runner_id, channel_name)
    project = runner_manual_case_project(runner_id, pivot_id)
    actor = _actor(runner_id, project)
    if action is ManualCaseActionKind.CLAIM:
        claim_manual_case(
            project=project, pivot_id=pivot_id, actor=actor, runner_id=runner_id
        )
        return _CLAIMED
    if action is ManualCaseActionKind.PAUSE:
        paused = pause_manual_case(
            project=project, pivot_id=pivot_id, actor=actor, claim_token=claim_token
        )
        return _PAUSED if paused else _ALREADY_PAUSED
    if action is ManualCaseActionKind.CONTINUE:
        continue_manual_case(
            project=project, pivot_id=pivot_id, actor=actor, claim_token=claim_token
        )
        return _CONTINUED
    release_manual_case(
        project=project, pivot_id=pivot_id, actor=actor, claim_token=claim_token
    )
    return _RELEASED


def progress_from_runner(
    *, runner_id: RunnerId, channel_name: str, payload: ManualCaseProgressPayload
) -> None:
    """Store the window's report of the tester's marks so far (no reply).

    Owner connection only, under its own per-runner throttle, for a case of a
    run assigned to the runner whose project the owner may write. Raises
    ``ManualRunError`` (user-safe; the consumer logs it).
    """
    if not owns_runner(runner_id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    if not hit_runner_throttle(
        f"projects:runner-manual-progress:{runner_id}",
        limit=_PROGRESS_LIMIT,
        window_seconds=_PROGRESS_WINDOW_SECONDS,
    ):
        raise ManualRunError(_PROGRESS_THROTTLED)
    project = runner_manual_case_project(runner_id, payload.pivot_id)
    _actor(runner_id, project)
    record_manual_progress(
        project=project,
        pivot_id=payload.pivot_id,
        claim_token=payload.claim_token,
        current_step=payload.current_step,
        steps=payload.steps,
    )


def submit_from_runner(
    *,
    runner_id: RunnerId,
    channel_name: str,
    payload: ManualCaseSubmitPayload,
) -> tuple[str, ...]:
    """Hand in the claimed case; the screenshot keys the server still needs.

    The stored hand-in records the runner (``ManualCaseSubmission.runner``)
    in the same transaction, so its screenshots are accepted only from it.
    Raises ``ManualRunError`` (user-safe), or ``ManualSubmissionDroppedError``
    when the runner must delete the hand-in.
    """
    _check_owner_and_throttle(runner_id, channel_name)
    try:
        project = runner_manual_case_project(runner_id, payload.pivot_id)
    except ManualRunError as exc:
        # The case left this runner for good: the hand-in can never land.
        raise ManualSubmissionDroppedError(exc.message) from exc
    actor = _actor(runner_id, project)
    with transaction.atomic():
        missing = submit_manual_case(project=project, payload=payload, actor=actor)
        ManualCaseSubmission.objects.filter(
            submission_id=uuid.UUID(payload.submission_id),
            test_run_test_case_id=payload.pivot_id,
            runner__isnull=True,
        ).update(runner_id=runner_id)
    return missing


def upload_from_runner(
    *, runner_id: RunnerId, channel_name: str, data: Mapping[str, object]
) -> None:
    """Parse and store one screenshot of one of the runner's hand-ins.

    The owner and the upload budget are checked before the base64 is decoded.
    Raises ``ManualRunError`` (user-safe; a retry may pass, e.g. throttled),
    ``ManualShotRejectedError`` when this shot can never be stored (also a
    malformed upload: bad base64, size or sha256; its hand-in is marked
    incomplete), or ``ManualSubmissionDroppedError`` when the whole hand-in
    is unknown, not this runner's, or superseded.
    """
    if not owns_runner(runner_id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    if not hit_runner_throttle(
        f"projects:runner-manual-shot:{runner_id}",
        limit=settings.MANUAL_SHOT_RATE_PER_MINUTE,
        window_seconds=_UPLOAD_WINDOW_SECONDS,
    ):
        raise ManualRunError(_UPLOAD_THROTTLED)
    ids = peek_manual_upload_ids(data)
    if ids is None:
        raise ManualSubmissionDroppedError(_UNKNOWN_HAND_IN)
    project = _upload_project(runner_id, ids[0])
    try:
        payload = parse_manual_screenshot_upload_payload(data)
    except ProtocolError as exc:
        # Bad base64, size or sha256 never pass on a retry: this shot is
        # rejected and its hand-in marked incomplete.
        reject_manual_shot(
            project=project,
            submission_id=ids[0],
            message=f"Screenshot refused: {exc}",
            runner_id=runner_id,
        )
    store_manual_screenshot(
        project=project,
        payload=payload,
        capture_source=ScreenshotCaptureSource.RUNNER,
        runner_id=runner_id,
    )


def _upload_project(runner_id: RunnerId, submission_id: str) -> Project:
    """The project of a hand-in the runner handed in (one query).

    Uploads are not an answer: the owner's membership is not re-checked (the
    hand-in was accepted while they could write). Raises
    ``ManualSubmissionDroppedError`` for an unknown or foreign hand-in.
    """
    try:
        submission_uuid = uuid.UUID(submission_id)
    except ValueError:
        raise ManualSubmissionDroppedError(_UNKNOWN_HAND_IN) from None
    project = Project.objects.filter(
        test_runs__pivot_entries__manual_submissions__submission_id=submission_uuid,
        test_runs__pivot_entries__manual_submissions__runner_id=runner_id,
    ).first()
    if project is None:
        raise ManualSubmissionDroppedError(_UNKNOWN_HAND_IN)
    return project
