"""The project's Test Runner acts on a Manual run (server-side seam).

The runner window claims and releases a case, hands it in, uploads its
screenshots and asks the AI about a step (``MANUAL_RUN_V1``); it also pauses
and continues the claimed case and reports the tester's marks so far
(``MANUAL_PAUSE_DISCUSS_V1``; the consumer checks the capability). Every rule
of those actions stays in the manual services (``manual_case_claim_services``,
``manual_case_pause_services``, ``manual_case_progress_services``,
``manual_case_submit_services``, ``manual_screenshot_services``,
``person_step_question_services.ask_manual_step_question``). This module
applies the rules of ``runner_person_step_services.answer_from_runner``: only
the connection that owns the project, a throttle, and the person named only
while the runner's email is an active writable member (else anonymous).

Throttles: claim, release, pause, continue and hand-in share the per-project
runner answer throttle (20/min, ``check_answer_throttle``); progress reports
have their own (``_PROGRESS_LIMIT`` per minute, so they never starve the
others); screenshot uploads have their
own per-project budget (``MANUAL_SHOT_RATE_PER_MINUTE``); Ask AI uses the
shared question throttle inside the question service.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.conf import settings

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
from projects.models import Project
from projects.person_step_actor import PersonStepActor
from projects.person_step_question_services import ask_manual_step_question

# The shared helpers of the runner's answers; the answer throttle key and
# limit must stay the same for every runner answer of a project.
from projects.runner_person_step_services import (
    check_answer_throttle,
    current_member,
    hit_runner_throttle,
    owns_project,
)

_NOT_OWNER = "This Test Runner is no longer the project's connection. Reconnect it."
_UPLOAD_THROTTLED = "Too many screenshots at once. The upload retries shortly."
_UPLOAD_WINDOW_SECONDS = 60
_CLAIMED = "Claimed. The case is yours."
_RELEASED = "Released. The case is back in the list."
_PAUSED = "Paused. The clock is stopped until you continue testing."
_ALREADY_PAUSED = "The case was already paused."
_CONTINUED = "Go on testing. The clock runs again."
_PROGRESS_THROTTLED = "Too many progress reports. The next one is sent shortly."
# Progress reports per project and minute (the window debounces them to one
# every two seconds at most while the tester marks steps, plus a flush before
# each pause or discussion message, so twice that rate leaves headroom).
_PROGRESS_LIMIT = 60
_PROGRESS_WINDOW_SECONDS = 60


def _check_owner_and_throttle(project_id: int, channel_name: str) -> None:
    if not owns_project(project_id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    try:
        check_answer_throttle(project_id)
    except PersonStepError as exc:
        raise ManualRunError(exc.message) from exc


def _actor(project_id: int, person_user_id: int | None) -> PersonStepActor:
    return PersonStepActor.runner(current_member(project_id, person_user_id))


def act_from_runner(
    *,
    project: Project,
    channel_name: str,
    person_user_id: int | None,
    action: ManualCaseActionKind,
    pivot_id: int,
    claim_token: str,
) -> str:
    """Claim, release, pause or continue one of the project's Manual run
    cases; the window's text.

    ``claim_token`` is the claim a release, pause or continue acts on (blank
    for a claim). Raises ``ManualRunError`` (user-safe).
    """
    _check_owner_and_throttle(project.id, channel_name)
    actor = _actor(project.id, person_user_id)
    if action is ManualCaseActionKind.CLAIM:
        claim_manual_case(project=project, pivot_id=pivot_id, actor=actor)
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
    *, project: Project, channel_name: str, payload: ManualCaseProgressPayload
) -> None:
    """Store the window's report of the tester's marks so far (no reply).

    Owner connection only, under its own per-project throttle. Raises
    ``ManualRunError`` (user-safe; the consumer logs it).
    """
    if not owns_project(project.id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    if not hit_runner_throttle(
        f"projects:runner-manual-progress:{project.id}",
        limit=_PROGRESS_LIMIT,
        window_seconds=_PROGRESS_WINDOW_SECONDS,
    ):
        raise ManualRunError(_PROGRESS_THROTTLED)
    record_manual_progress(
        project=project,
        pivot_id=payload.pivot_id,
        claim_token=payload.claim_token,
        current_step=payload.current_step,
        steps=payload.steps,
    )


def submit_from_runner(
    *,
    project: Project,
    channel_name: str,
    person_user_id: int | None,
    payload: ManualCaseSubmitPayload,
) -> tuple[str, ...]:
    """Hand in the claimed case; the screenshot keys the server still needs.

    Raises ``ManualRunError`` (user-safe), or ``ManualSubmissionDroppedError``
    when the runner must delete the hand-in.
    """
    _check_owner_and_throttle(project.id, channel_name)
    return submit_manual_case(
        project=project, payload=payload, actor=_actor(project.id, person_user_id)
    )


def upload_from_runner(
    *, project: Project, channel_name: str, data: Mapping[str, object]
) -> None:
    """Parse and store one screenshot of a hand-in.

    The owner and the upload budget are checked before the base64 is decoded.
    Raises ``ManualRunError`` (user-safe; a retry may pass, e.g. throttled),
    ``ManualShotRejectedError`` when this shot can never be stored (also a
    malformed upload: bad base64, size or sha256; its hand-in is marked
    incomplete), or ``ManualSubmissionDroppedError`` when the whole hand-in
    is unknown or superseded.
    """
    if not owns_project(project.id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    if not hit_runner_throttle(
        f"projects:runner-manual-shot:{project.id}",
        limit=settings.MANUAL_SHOT_RATE_PER_MINUTE,
        window_seconds=_UPLOAD_WINDOW_SECONDS,
    ):
        raise ManualRunError(_UPLOAD_THROTTLED)
    try:
        payload = parse_manual_screenshot_upload_payload(data)
    except ProtocolError as exc:
        # Bad base64, size or sha256 never pass on a retry: this shot is
        # rejected and its hand-in marked incomplete.
        message = f"Screenshot refused: {exc}"
        ids = peek_manual_upload_ids(data)
        if ids is None:
            raise ManualShotRejectedError(message) from exc
        reject_manual_shot(project=project, submission_id=ids[0], message=message)
    store_manual_screenshot(project=project, payload=payload)


def ask_manual_from_runner(
    *,
    project: Project,
    channel_name: str,
    person_user_id: int | None,
    pivot_id: int,
    claim_token: str,
    step_num: int | None,
    question: str,
) -> None:
    """Store the window's Ask AI question about the claimed case.

    Throttled by the shared question throttle inside the question service.
    Raises ``ManualRunError`` (or ``PersonStepQuestionError``; user-safe).
    """
    if not owns_project(project.id, channel_name):
        raise ManualRunError(_NOT_OWNER)
    ask_manual_step_question(
        project=project,
        pivot_id=pivot_id,
        claim_token=claim_token,
        step_num=step_num,
        question=question,
        actor=_actor(project.id, person_user_id),
    )
