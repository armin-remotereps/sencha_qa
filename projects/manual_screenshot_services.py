"""Use case: store one screenshot of a Manual run hand-in from the Test Runner.

The runner uploads the shots a hand-in announced (``shot_manifest``) one at
a time, after the hand-in. Each is checked (size, a real PNG or JPEG of the
announced type within the pixel cap) before any lock, then, under the locks,
against its announcement (key, hash, size), and stored as a
``TestRunScreenshot`` keyed by ``(case, manual_key)``, so a repeated upload
answers ok without writing. Locks: the case, then the hand-in (Re-run and
Reset update the case rows before they supersede its hand-ins, so an upload
never lands on a discarded hand-in). When the last shot lands the hand-in is
complete and its AI rewrite is queued (``queue_manual_refinement``).

A shot that can never be stored is rejected alone (``ManualShotRejectedError``:
the runner deletes that shot, the others still upload). Its hand-in can then
never be complete, so it goes ``incomplete`` at once (checkable without
waiting for the stale sweep) and its rewrite is queued.

The owner connection and the upload rate are checked by the caller
(``runner_manual_services``); everything here is scoped to ``project`` and,
for a runner upload (``runner_id``), to the hand-ins that runner handed in
(``ManualCaseSubmission.runner``): another runner can never upload into them.
"""

from __future__ import annotations

import io
import uuid
from typing import Final, NoReturn

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from controller_client.protocol import (
    MANUAL_SHOT_MAX_PIXELS,
    ManualScreenshotUploadPayload,
    ManualShotKind,
    ManualShotMime,
)
from projects.exceptions import ManualShotRejectedError, ManualSubmissionDroppedError
from projects.manual_refinement_services import queue_manual_refinement
from projects.manual_run_policy import MANUAL_SHOT_TOOL_NAMES, manual_attempt_id
from projects.manual_screenshot_push import broadcast_manual_screenshot
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    Project,
    ScreenshotCaptureSource,
    TestRunScreenshot,
    TestRunTestCase,
)
from projects.runner_person_step_push import nudge_runner_person_steps
from runners.types import RunnerId

_PIL_FORMATS: Final[dict[ManualShotMime, str]] = {
    ManualShotMime.PNG: "PNG",
    ManualShotMime.JPEG: "JPEG",
}
_EXTENSIONS: Final[dict[ManualShotMime, str]] = {
    ManualShotMime.PNG: "png",
    ManualShotMime.JPEG: "jpg",
}

_TOO_LARGE = "This screenshot is larger than the server accepts."
_NOT_AN_IMAGE = "This screenshot is not a valid PNG or JPEG image."
_TOO_MANY_PIXELS = "This screenshot has too many pixels."
_UNKNOWN = "This hand-in is not known for this project any more."
_SUPERSEDED = "This case was re-run or reset; its screenshots were discarded."
_NOT_LISTED = "This screenshot was not announced by its hand-in."
_MISMATCH = "This screenshot does not match the one its hand-in announced."


def store_manual_screenshot(
    *,
    project: Project,
    payload: ManualScreenshotUploadPayload,
    image_checked: bool = False,
    capture_source: str = "",
    runner_id: RunnerId | None = None,
) -> str | None:
    """Store one screenshot of a hand-in of ``project`` (idempotent); the
    stored file's name, or None when nothing was written.

    ``image_checked`` skips ``check_manual_image`` for bytes the caller
    already passed through it (the MCP gateway checks each image when it is
    staged); the key, sha256 and size are still matched against the
    announcement. The runner upload leaves it False.

    ``capture_source`` (a ``ScreenshotCaptureSource`` value, internal) is
    written on the stored row: ``"runner"`` for shots captured on the test
    machine, ``""`` when unknown. Raises ``ValueError`` for any other value.

    ``runner_id``: a Test Runner's upload; only hand-ins it handed in are
    known to it. None for the MCP gateway's own hand-ins.

    Raises ``ManualSubmissionDroppedError`` when the hand-in is unknown to the
    project or superseded (the runner deletes the whole hand-in) and
    ``ManualShotRejectedError`` when this shot can never be stored (too large,
    not a valid image of the announced type within the pixel cap, not
    announced, not matching its announcement); the hand-in is then marked
    incomplete first.
    """
    source = ScreenshotCaptureSource(capture_source)
    submission_uuid = uuid.UUID(payload.submission_id)
    pivot_id = _submission_pivot_id(project, submission_uuid, runner_id)
    if not image_checked:
        reason = check_manual_image(payload.mime, payload.data)
        if reason:
            reject_manual_shot(
                project=project,
                submission_id=payload.submission_id,
                message=reason,
                runner_id=runner_id,
            )

    rejected = ""
    stored_name: str | None = None
    with transaction.atomic():
        pivot, submission = _lock_current(pivot_id, submission_uuid)
        entry = _manifest_entry(submission.shot_manifest, payload.key)
        if entry is None:
            rejected = _NOT_LISTED
        elif entry.get("sha256") != payload.sha256 or entry.get("size") != len(
            payload.data
        ):
            rejected = _MISMATCH
        if entry is None or rejected:
            _mark_incomplete(submission)
        elif not TestRunScreenshot.objects.filter(
            test_run_test_case_id=pivot.pk, manual_key=payload.key
        ).exists():
            stored_name = _store(pivot, submission, entry, payload, source)
    # Raised after the commit, so the incomplete mark is kept.
    if rejected:
        raise ManualShotRejectedError(rejected)
    return stored_name


def reject_manual_shot(
    *,
    project: Project,
    submission_id: str,
    message: str,
    runner_id: RunnerId | None = None,
) -> NoReturn:
    """Refuse one screenshot for good and mark its hand-in incomplete.

    For a refusal found before the shot could be matched to its hand-in
    (oversized or malformed upload, not an image). Always raises:
    ``ManualSubmissionDroppedError`` when the hand-in is unknown to the
    project or superseded, else ``ManualShotRejectedError(message)`` once the
    hand-in is marked. ``runner_id`` as for ``store_manual_screenshot``.
    """
    submission_uuid = uuid.UUID(submission_id)
    pivot_id = _submission_pivot_id(project, submission_uuid, runner_id)
    with transaction.atomic():
        _pivot, submission = _lock_current(pivot_id, submission_uuid)
        _mark_incomplete(submission)
    raise ManualShotRejectedError(message)


def _submission_pivot_id(
    project: Project, submission_uuid: uuid.UUID, runner_id: RunnerId | None
) -> int:
    """The case of one of ``project``'s hand-ins, handed in by ``runner_id``
    when given (one query)."""
    submissions = ManualCaseSubmission.objects.filter(
        submission_id=submission_uuid,
        test_run_test_case__test_run__project_id=project.pk,
    )
    if runner_id is not None:
        submissions = submissions.filter(runner_id=runner_id)
    pivot_id = submissions.values_list("test_run_test_case_id", flat=True).first()
    if pivot_id is None:
        raise ManualSubmissionDroppedError(_UNKNOWN)
    return pivot_id


def _lock_current(
    pivot_id: int, submission_uuid: uuid.UUID
) -> tuple[TestRunTestCase, ManualCaseSubmission]:
    """Lock the case, then the hand-in; refuse a superseded hand-in."""
    pivot = (
        TestRunTestCase.objects.select_for_update()
        .only("id", "test_run_id")
        .get(pk=pivot_id)
    )
    submission = ManualCaseSubmission.objects.select_for_update().get(
        submission_id=submission_uuid
    )
    if submission.upload_state == ManualUploadState.SUPERSEDED:
        raise ManualSubmissionDroppedError(_SUPERSEDED)
    return pivot, submission


def _mark_incomplete(submission: ManualCaseSubmission) -> None:
    """An uploading hand-in lost a shot for good: incomplete now (checkable),
    and its rewrite is queued once (on the first such shot)."""
    if submission.upload_state != ManualUploadState.UPLOADING:
        return
    submission.upload_state = ManualUploadState.INCOMPLETE
    submission.save(update_fields=["upload_state"])
    queue_manual_refinement(submission.pk)
    # The hand-in's runner snapshot's pending uploads changed.
    runner_id = _submission_runner_id(submission)
    transaction.on_commit(lambda: nudge_runner_person_steps(runner_id))


def _store(
    pivot: TestRunTestCase,
    submission: ManualCaseSubmission,
    entry: dict[str, object],
    payload: ManualScreenshotUploadPayload,
    capture_source: ScreenshotCaptureSource,
) -> str:
    """Write the screenshot and count it on the locked hand-in; the stored
    file's name (a caller whose outer transaction rolls back deletes it)."""
    kind = ManualShotKind(str(entry.get("kind")))
    raw_step = entry.get("step_num")
    # The file is written before the row, as for every screenshot; a rollback
    # leaves only an orphan file (the caller deletes the returned name). A
    # failed insert deletes its own file, whose name the caller never gets.
    screenshot = TestRunScreenshot(
        test_run_test_case=pivot,
        tool_name=MANUAL_SHOT_TOOL_NAMES[kind],
        # A step shot carries its step's attempt id: named by the step's first
        # shot (``attempt_key``; hand-ins stored before it had one shot per
        # step, named by the shot itself).
        attempt_id=(
            manual_attempt_id(_attempt_key(entry, payload.key))
            if kind is not ManualShotKind.CASE
            else ""
        ),
        step_num=raw_step if isinstance(raw_step, int) else None,
        manual_key=payload.key,
        capture_source=capture_source,
    )
    screenshot.image.save(
        f"manual_{payload.key}.{_EXTENSIONS[payload.mime]}",
        ContentFile(payload.data),
        save=False,
    )
    try:
        screenshot.save()
        submission.shots_received = min(
            submission.shots_received + 1, submission.shots_expected
        )
        submission.last_shot_at = timezone.now()
        completed = submission.shots_received >= submission.shots_expected
        if completed:
            # Also from ``incomplete``: a late shot still completes it.
            submission.upload_state = ManualUploadState.COMPLETE
        submission.save(
            update_fields=["shots_received", "last_shot_at", "upload_state"]
        )
    except Exception:
        screenshot.image.delete(save=False)
        raise
    pivot_id = pivot.pk
    transaction.on_commit(lambda: broadcast_manual_screenshot(pivot_id, screenshot))
    if completed:
        queue_manual_refinement(submission.pk)
        runner_id = _submission_runner_id(submission)
        transaction.on_commit(lambda: nudge_runner_person_steps(runner_id))
    return str(screenshot.image.name)


def _submission_runner_id(submission: ManualCaseSubmission) -> RunnerId | None:
    """The runner that handed the hand-in in (no query); None through MCP."""
    raw = submission.runner_id
    return RunnerId(raw) if raw is not None else None


def check_manual_image(mime: ManualShotMime, data: bytes) -> str:
    """Why the bytes are not storable as a manual screenshot ("" when they
    are; no query). Shared by the runner upload and the MCP gateway."""
    reason, image = load_manual_image(mime, data)
    if image is not None:
        image.close()
    return reason


def load_manual_image(
    mime: ManualShotMime, data: bytes
) -> tuple[str, Image.Image | None]:
    """``check_manual_image`` that keeps the decode: ("", the fully loaded
    image) when the bytes are storable, else (the reason, None). The caller
    closes the image.

    The size first, then the header (format and pixel count, nothing
    decoded), then ``verify()`` and a full ``load()`` on a fresh handle.
    """
    if len(data) > settings.MANUAL_SHOT_MAX_BYTES:
        return _TOO_LARGE, None
    expected_format = _PIL_FORMATS[mime]
    try:
        with Image.open(io.BytesIO(data), formats=[expected_format]) as header:
            if header.format != expected_format:
                return _NOT_AN_IMAGE, None
            width, height = header.size
            if width * height > MANUAL_SHOT_MAX_PIXELS:
                return _TOO_MANY_PIXELS, None
            header.verify()
        image = Image.open(io.BytesIO(data), formats=[expected_format])
    except Image.DecompressionBombError:
        return _TOO_MANY_PIXELS, None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return _NOT_AN_IMAGE, None
    try:
        image.load()
    except Image.DecompressionBombError:
        image.close()
        return _TOO_MANY_PIXELS, None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        image.close()
        return _NOT_AN_IMAGE, None
    return "", image


def _attempt_key(entry: dict[str, object], key: str) -> str:
    """The screenshot key naming the attempt of a step shot's manifest entry."""
    attempt_key = entry.get("attempt_key")
    return attempt_key if isinstance(attempt_key, str) and attempt_key else key


def _manifest_entry(manifest: object, key: str) -> dict[str, object] | None:
    """The announced shot ``key`` of a stored ``shot_manifest``; None if absent."""
    if not isinstance(manifest, list):
        return None
    for entry in manifest:
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    return None
