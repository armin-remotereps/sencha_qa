"""The Test Runner window's Manual run snapshot (``manual_run_state``).

Read side only. The snapshot lists the project's running Manual run with its
cases (capped, run order), the claimed case with its verbatim steps from the
checkpoint the claim wrote, and every hand-in of the project still waiting
for screenshots, with the keys the server misses, so a reconnected runner
resumes its uploads. A runner with MANUAL_PAUSE_DISCUSS_V1 also gets the
claimed case's pause fields and sees it as ``paused`` in the list (no extra
query); the case's discussion is composed in by
``projects.runner_window_state``.
A constant number of queries (at most five), whatever the number of cases.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from agents.checkpoint import checkpoint_from_json
from controller_client.protocol import (
    MANUAL_CASE_STATUS_MAX_CHARS,
    MANUAL_CASE_TITLE_MAX_CHARS,
    MANUAL_PENDING_UPLOADS_MAX,
    MANUAL_PRECONDITIONS_MAX_CHARS,
    MANUAL_RUN_STATE_MAX_CASES,
    MANUAL_STEP_TEXT_MAX_CHARS,
    MANUAL_TESTRAIL_ID_MAX_CHARS,
    PERSON_STEP_LABEL_MAX_CHARS,
    ClientCapability,
    ManualActiveCasePayload,
    ManualCaseListItemPayload,
    ManualPendingUploadPayload,
    ManualRunInfoPayload,
    ManualRunStateMessage,
    ManualStepPayload,
)
from projects.case_markup import markup_to_plain
from projects.manual_case_pause_services import get_manual_pause_panel
from projects.manual_case_steps import manual_steps_from_checkpoint
from projects.manual_case_submit_services import manifest_keys
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    Project,
    RunHelpMode,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)
from projects.run_case_order import RUN_CASE_ORDERING

# The list status of the claimed case while it is paused (MANUAL_PAUSE_DISCUSS_V1).
_PAUSED_LIST_STATUS: Final = "paused"


def build_manual_run_state(
    *, project: Project, runner_capabilities: frozenset[str]
) -> ManualRunStateMessage:
    """The ``manual_run_state`` for ``project``'s MANUAL_RUN_V1 runner window.

    ``run`` is None when the project has no running Manual run (the window
    leaves the manual view); ``pending_uploads`` is listed either way. The
    claimed case is the run's one claim (claims belong to the project's one
    runner connection, so the snapshot is the same for every person at it).
    The pause fields and the ``paused`` list status only for a runner that
    also advertised MANUAL_PAUSE_DISCUSS_V1 (older windows get the snapshot
    unchanged).
    """
    run = (
        TestRun.objects.filter(
            project_id=project.pk,
            help_mode=RunHelpMode.MANUAL,
            status=TestRunStatus.STARTED,
        )
        # Status and mode: the pause panel (both known from the filter).
        .only("id", "name", "status", "help_mode")
        .order_by("-started_at", "-pk")
        .first()
    )
    pending = _pending_uploads(project.pk)
    if run is None:
        return ManualRunStateMessage(
            run=None, cases=(), active=None, pending_uploads=pending
        )
    pause_discuss = (
        ClientCapability.MANUAL_PAUSE_DISCUSS_V1.value in runner_capabilities
    )
    rows = list(
        TestRunTestCase.objects.filter(test_run_id=run.pk)
        .order_by(*RUN_CASE_ORDERING)
        .values_list(
            "id",
            "status",
            "manual_claimed_label",
            "manual_paused_at",
            "test_case__title",
            "test_case__testrail_id",
        )[:MANUAL_RUN_STATE_MAX_CASES]
    )
    cases = tuple(
        ManualCaseListItemPayload(
            pivot_id=pivot_id,
            title=str(title)[:MANUAL_CASE_TITLE_MAX_CHARS],
            testrail_id=str(testrail_id or "")[:MANUAL_TESTRAIL_ID_MAX_CHARS],
            status=_list_status(
                str(status), paused=pause_discuss and paused_at is not None
            )[:MANUAL_CASE_STATUS_MAX_CHARS],
            claimed_label=str(label or "")[:PERSON_STEP_LABEL_MAX_CHARS],
        )
        for pivot_id, status, label, paused_at, title, testrail_id in rows
    )
    return ManualRunStateMessage(
        run=ManualRunInfoPayload(
            run_id=run.pk,
            name=(run.name or f"Run #{run.pk}")[:PERSON_STEP_LABEL_MAX_CHARS],
        ),
        cases=cases,
        active=_active_case(run, pause_discuss=pause_discuss),
        pending_uploads=pending,
    )


def _list_status(status: str, *, paused: bool) -> str:
    """The list status of a case; ``paused`` only for the claimed case while
    it is paused (and only for a MANUAL_PAUSE_DISCUSS_V1 window)."""
    if paused and status == TestRunTestCaseStatus.IN_PROGRESS:
        return _PAUSED_LIST_STATUS
    return status


def _active_case(
    test_run: TestRun, *, pause_discuss: bool
) -> ManualActiveCasePayload | None:
    """The run's claimed case with its verbatim steps (one query; the pause
    fields with ``pause_discuss`` come from loaded fields)."""
    claimed = (
        TestRunTestCase.objects.filter(
            test_run_id=test_run.pk, status=TestRunTestCaseStatus.IN_PROGRESS
        )
        .exclude(manual_claim_token="")
        .select_related("test_case")
        .only(
            "id",
            "status",
            "manual_claim_token",
            "manual_claimed_at",
            "manual_paused_at",
            "manual_paused_label",
            "execution_checkpoint",
            "test_case__title",
            "test_case__preconditions",
        )
        .first()
    )
    if claimed is None:
        return None
    checkpoint = checkpoint_from_json(claimed.execution_checkpoint)
    steps = manual_steps_from_checkpoint(checkpoint) if checkpoint is not None else ()
    active = ManualActiveCasePayload(
        pivot_id=claimed.pk,
        claim_token=claimed.manual_claim_token,
        title=claimed.test_case.title[:MANUAL_CASE_TITLE_MAX_CHARS],
        preconditions=markup_to_plain(claimed.test_case.preconditions)[
            :MANUAL_PRECONDITIONS_MAX_CHARS
        ],
        steps=tuple(
            ManualStepPayload(
                num=step.num,
                text=markup_to_plain(step.text)[:MANUAL_STEP_TEXT_MAX_CHARS],
                expected=markup_to_plain(step.expected)[:MANUAL_STEP_TEXT_MAX_CHARS],
            )
            for step in steps
        ),
        claimed_at=(
            claimed.manual_claimed_at.isoformat()
            if claimed.manual_claimed_at is not None
            else ""
        ),
    )
    if not pause_discuss:
        return active
    panel = get_manual_pause_panel(test_run, claimed)
    return replace(
        active,
        paused=panel.paused,
        paused_by=panel.paused_label[:PERSON_STEP_LABEL_MAX_CHARS],
        paused_at=panel.paused_at.isoformat() if panel.paused_at is not None else "",
        can_pause=panel.can_pause,
        can_continue=panel.can_continue,
        pause_blocker=panel.blocker,
    )


def _pending_uploads(project_id: int) -> tuple[ManualPendingUploadPayload, ...]:
    """The project's hand-ins still uploading, newest first (capped), with
    their missing keys (two queries; one when there are none)."""
    submissions = list(
        ManualCaseSubmission.objects.filter(
            test_run_test_case__test_run__project_id=project_id,
            # An ``incomplete`` one is resumed by the runner's own replay of
            # the hand-in, which answers its missing keys.
            upload_state=ManualUploadState.UPLOADING,
        )
        .order_by("-submitted_at", "-pk")
        .values_list("submission_id", "test_run_test_case_id", "shot_manifest")[
            :MANUAL_PENDING_UPLOADS_MAX
        ]
    )
    if not submissions:
        return ()
    keys_by_submission = [
        (submission_id, pivot_id, manifest_keys(manifest))
        for submission_id, pivot_id, manifest in submissions
    ]
    all_keys = {key for _sid, _pid, keys in keys_by_submission for key in keys}
    stored = set(
        TestRunScreenshot.objects.filter(
            test_run_test_case_id__in={pid for _sid, pid, _keys in keys_by_submission},
            manual_key__in=all_keys,
        ).values_list("test_run_test_case_id", "manual_key")
    )
    return tuple(
        ManualPendingUploadPayload(
            submission_id=str(submission_id),
            missing=tuple(key for key in keys if (pivot_id, key) not in stored),
        )
        for submission_id, pivot_id, keys in keys_by_submission
    )
