"""Use case: the MCP tester hands in its claimed case (hand_in).

One transaction, under the locks ``submit_manual_case`` takes, in its order
(project, run, case: ``lock_manual_run_case``; re-taken by the services
below in the same transaction):

1. A hand-in already stored under this call's ``submission_id`` (uuid5 of
   pivot, claim token and the client's idempotency key) answers the replay,
   also when a concurrent call with the same key committed while this one
   waited for the locks. Then the claim must still be this client's and not
   paused.
2. The ``manual_case_submit`` wire dict is built from the claim's marks and
   bound shots, exactly as the Test Runner window sends it: one entry per
   marked step (its shot keys, note, seconds), a manifest entry per shot
   (key, step, kind, sha256, size), the optional case screenshot; unmarked
   steps are left out ("Not run"). ``parse_manual_case_submit_payload``
   checks it, so the note and structure rules stay single-sourced.
3. ``submit_manual_case`` (the neutral tester; it copies the claim's source
   onto the hand-in), then ``store_manual_screenshot`` for every shot from
   the staged bytes (``image_checked``: each image was fully checked when it
   was staged; key, hash and size are still matched; the staged
   ``capture_source`` is carried over), then the claim's
   staging rows are deleted. The hand-in therefore commits complete, never
   "uploading"; an unexpected shot refusal rolls the whole hand-in back
   (retryable), and the files already written are deleted, since a rollback
   cannot remove them.

``ManualRunError`` is retryable; ``ManualSubmissionDroppedError`` is final
and the claim's staging rows are deleted.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Final

from django.core.files.storage import default_storage
from django.db import transaction

from controller_client.protocol import (
    ManualScreenshotUploadPayload,
    ManualShotKind,
    ManualShotMime,
    parse_manual_case_submit_payload,
)
from mcp_gateway.exceptions import McpToolError
from mcp_gateway.models import McpStagedShot, McpStepMark
from mcp_gateway.schemas import HandInRequest, HandInResponse
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.own_claim import (
    check_own_claim,
    delete_claim_staging,
    refuse_paused,
)
from mcp_gateway.tasks import stop_claim_commands
from projects.exceptions import ManualSubmissionDroppedError
from projects.manual_case_lock import lock_manual_run_case
from projects.manual_case_submit_services import submit_manual_case
from projects.manual_screenshot_services import store_manual_screenshot
from projects.models import (
    ManualCaseSubmission,
    TestRunStatus,
    TestRunTestCase,
)

logger = logging.getLogger(__name__)

# Fixed namespace of MCP hand-in ids (uuid5 of pivot, claim and the client's
# idempotency key).
_SUBMISSION_NAMESPACE: Final = uuid.UUID("5b0c1f9e-6a43-4d55-9a8e-2f6c3e1d7a10")
_HANDED_IN: Final = "Handed in."
_REPLAYED: Final = "Already handed in."
_UNKNOWN_CASE_SHOT: Final = (
    "case_shot_id is not a screenshot of this claim, or a step already uses it."
)
_STEP_WITHOUT_SHOTS: Final = (
    "Step {num} has no screenshot any more. Mark it again with a screenshot."
)
_SHOT_GONE: Final = (
    "A screenshot of this case is no longer available. Call get_case to see "
    "the case's state."
)


def _submission_id(pivot_id: int, claim_token: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(_SUBMISSION_NAMESPACE, f"{pivot_id}:{claim_token}:{idempotency_key}")
    )


def hand_in(call: McpCall, request: HandInRequest) -> HandInResponse:
    submission_id = _submission_id(
        request.pivot_id, request.claim_token, request.idempotency_key
    )
    # A retried call after a committed hand-in: no locks needed.
    if _already_handed_in(call, request, submission_id):
        return _replayed(request.pivot_id)

    stored_names: list[str] = []
    # Set by the first on_commit callback: a later callback that raises
    # (a broadcast) must not delete the files of a committed hand-in.
    committed: list[bool] = []

    def discard_files() -> None:
        if not committed:
            _delete_files(stored_names)

    try:
        with transaction.atomic():
            transaction.on_commit(lambda: committed.append(True))
            test_run, pivot = lock_manual_run_case(
                project_id=call.project.pk, pivot_id=request.pivot_id
            )
            # A concurrent call with the same key committed while this one
            # waited for the locks.
            if _already_handed_in(call, request, submission_id):
                return _replayed(request.pivot_id)
            refuse_paused(check_own_claim(test_run, pivot, request.claim_token))
            wire, shot_rows = _wire_submit(request, submission_id)
            payload = parse_manual_case_submit_payload(wire)
            submit_manual_case(project=call.project, payload=payload, actor=call.actor)
            for row_id, key, mime, sha256, capture_source in shot_rows:
                # One image in memory at a time.
                data = bytes(
                    McpStagedShot.objects.filter(pk=row_id)
                    .values_list("data", flat=True)
                    .get()
                )
                stored_name = store_manual_screenshot(
                    project=call.project,
                    payload=ManualScreenshotUploadPayload(
                        submission_id=submission_id,
                        key=key,
                        sha256=sha256,
                        mime=ManualShotMime(mime),
                        data=data,
                    ),
                    image_checked=True,
                    capture_source=capture_source,
                )
                if stored_name:
                    stored_names.append(stored_name)
            delete_claim_staging(request.pivot_id, request.claim_token)
            _stop_commands_on_commit(call.project.pk, request.pivot_id)
    except ManualSubmissionDroppedError:
        discard_files()
        delete_claim_staging(request.pivot_id, request.claim_token)
        raise
    except McpStagedShot.DoesNotExist as exc:
        # Under the case lock staged rows only vanish with the claim; if the
        # hand-in landed under this key meanwhile, answer the replay.
        discard_files()
        if _already_handed_in(call, request, submission_id):
            return _replayed(request.pivot_id)
        raise McpToolError(_SHOT_GONE) from exc
    except BaseException:
        # The transaction rolled back: the rows are gone, the files are not.
        discard_files()
        raise
    return HandInResponse(
        message=_HANDED_IN, run_finished=_run_finished(request.pivot_id)
    )


def _stop_commands_on_commit(project_id: int, pivot_id: int) -> None:
    """After the commit, stop the ended claim's kept commands (D7, best
    effort; ``robust``: a broker fault never fails the hand-in, the minute
    sweep catches up)."""
    transaction.on_commit(
        lambda: stop_claim_commands.delay(project_id, pivot_id), robust=True
    )


def _already_handed_in(
    call: McpCall, request: HandInRequest, submission_id: str
) -> bool:
    """A hand-in of this case is stored under ``submission_id`` (one query)."""
    return ManualCaseSubmission.objects.filter(
        submission_id=uuid.UUID(submission_id),
        test_run_test_case_id=request.pivot_id,
        test_run_test_case__test_run__project_id=call.project.pk,
    ).exists()


def _replayed(pivot_id: int) -> HandInResponse:
    return HandInResponse(message=_REPLAYED, run_finished=_run_finished(pivot_id))


def _delete_files(names: list[str]) -> None:
    """Delete screenshot files whose rows were rolled back (the default
    storage, as ``TestRunScreenshot.image``); never raises."""
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:
            logger.exception("could not delete a rolled-back manual screenshot")


def _wire_submit(
    request: HandInRequest, submission_id: str
) -> tuple[dict[str, object], list[tuple[int, str, str, str, str]]]:
    """The ``manual_case_submit`` dict and the shots to store (row id, key,
    mime, sha256, capture source), in manifest order (three queries)."""
    marks = list(
        McpStepMark.objects.filter(
            pivot_id=request.pivot_id, claim_token=request.claim_token
        )
        .order_by("step_num")
        .values_list("step_num", "outcome", "note", "seconds")
    )
    staged = list(
        McpStagedShot.objects.filter(
            pivot_id=request.pivot_id, claim_token=request.claim_token
        )
        .order_by("created_at", "pk")
        .values_list(
            "id",
            "shot_key",
            "mime",
            "sha256",
            "size",
            "step_num",
            "kind",
            "capture_source",
        )
    )
    by_step: defaultdict[int, list[tuple[int, str, str, str, int, str, str]]] = (
        defaultdict(list)
    )
    unbound: dict[str, tuple[int, str, str, str, int, str]] = {}
    for row_id, key, mime, sha256, size, step_num, kind, source in staged:
        if step_num is None:
            unbound[key] = (row_id, key, mime, sha256, size, source)
        else:
            by_step[step_num].append((row_id, key, mime, sha256, size, kind, source))

    steps: list[dict[str, object]] = []
    manifest: list[dict[str, object]] = []
    to_store: list[tuple[int, str, str, str, str]] = []
    for num, outcome, note, seconds in marks:
        shots = by_step.get(num, [])
        if not shots:
            raise McpToolError(_STEP_WITHOUT_SHOTS.format(num=num))
        steps.append(
            {
                "num": num,
                "outcome": outcome,
                "note": note,
                "shots": [key for _rid, key, _m, _s, _z, _k, _c in shots],
                "seconds": seconds,
            }
        )
        for row_id, key, mime, sha256, size, kind, source in shots:
            manifest.append(
                {
                    "key": key,
                    "step_num": num,
                    "kind": kind,
                    "sha256": sha256,
                    "size": size,
                }
            )
            to_store.append((row_id, key, mime, sha256, source))
    if request.case_shot_id:
        case_shot = unbound.get(request.case_shot_id)
        if case_shot is None:
            raise McpToolError(_UNKNOWN_CASE_SHOT)
        row_id, key, mime, sha256, size, source = case_shot
        manifest.append(
            {
                "key": key,
                "step_num": None,
                "kind": ManualShotKind.CASE.value,
                "sha256": sha256,
                "size": size,
            }
        )
        to_store.append((row_id, key, mime, sha256, source))
    wire: dict[str, object] = {
        "submission_id": submission_id,
        "pivot_id": request.pivot_id,
        "claim_token": request.claim_token,
        "steps": steps,
        "verdict": request.verdict,
        "verdict_note": request.verdict_note,
        "shots": manifest,
    }
    return wire, to_store


def _run_finished(pivot_id: int) -> bool:
    """The case's run is no longer running (one query)."""
    status = (
        TestRunTestCase.objects.filter(pk=pivot_id)
        .values_list("test_run__status", flat=True)
        .first()
    )
    return status is not None and status != TestRunStatus.STARTED
