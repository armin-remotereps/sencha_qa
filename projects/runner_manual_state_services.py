"""The Test Runner window's Manual run snapshot (``manual_run_state``).

Read side only. The snapshot lists the project's running Manual run with its
cases (capped, run order), the claimed case with its verbatim steps from the
checkpoint the claim wrote (and its Ask AI questions), and every hand-in of
the project still waiting for screenshots, with the keys the server misses,
so a reconnected runner resumes its uploads. A constant number of queries
(at most six), whatever the number of cases.
"""

from __future__ import annotations

from dataclasses import replace

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
    PersonStepQuestionPayload,
)
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
from projects.person_step_question_services import get_manual_ask_state
from projects.run_case_order import RUN_CASE_ORDERING


def build_manual_run_state(
    *, project: Project, runner_capabilities: frozenset[str]
) -> ManualRunStateMessage:
    """The ``manual_run_state`` for ``project``'s MANUAL_RUN_V1 runner window.

    ``run`` is None when the project has no running Manual run (the window
    leaves the manual view); ``pending_uploads`` is listed either way. The
    claimed case is the run's one claim (claims belong to the project's one
    runner connection, so the snapshot is the same for every person at it).
    Questions and ``can_ask`` only for a runner that also
    advertised PERSON_STEP_ASSIST_V1.
    """
    run = (
        TestRun.objects.filter(
            project_id=project.pk,
            help_mode=RunHelpMode.MANUAL,
            status=TestRunStatus.STARTED,
        )
        .only("id", "name")
        .order_by("-started_at", "-pk")
        .first()
    )
    pending = _pending_uploads(project.pk)
    if run is None:
        return ManualRunStateMessage(
            run=None, cases=(), active=None, pending_uploads=pending
        )
    rows = list(
        TestRunTestCase.objects.filter(test_run_id=run.pk)
        .order_by(*RUN_CASE_ORDERING)
        .values_list(
            "id",
            "status",
            "manual_claimed_label",
            "test_case__title",
            "test_case__testrail_id",
        )[:MANUAL_RUN_STATE_MAX_CASES]
    )
    cases = tuple(
        ManualCaseListItemPayload(
            pivot_id=pivot_id,
            title=str(title)[:MANUAL_CASE_TITLE_MAX_CHARS],
            testrail_id=str(testrail_id or "")[:MANUAL_TESTRAIL_ID_MAX_CHARS],
            status=str(status)[:MANUAL_CASE_STATUS_MAX_CHARS],
            claimed_label=str(label or "")[:PERSON_STEP_LABEL_MAX_CHARS],
        )
        for pivot_id, status, label, title, testrail_id in rows
    )
    return ManualRunStateMessage(
        run=ManualRunInfoPayload(
            run_id=run.pk,
            name=(run.name or f"Run #{run.pk}")[:PERSON_STEP_LABEL_MAX_CHARS],
        ),
        cases=cases,
        active=_active_case(
            run.pk,
            assist=ClientCapability.PERSON_STEP_ASSIST_V1.value in runner_capabilities,
        ),
        pending_uploads=pending,
    )


def _active_case(test_run_id: int, *, assist: bool) -> ManualActiveCasePayload | None:
    """The run's claimed case with its verbatim steps (one query, plus one for
    its questions with ``assist``)."""
    claimed = (
        TestRunTestCase.objects.filter(
            test_run_id=test_run_id, status=TestRunTestCaseStatus.IN_PROGRESS
        )
        .exclude(manual_claim_token="")
        .select_related("test_case")
        .only(
            "id",
            "manual_claim_token",
            "manual_claimed_at",
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
    questions: tuple[PersonStepQuestionPayload, ...] = ()
    can_ask = False
    if assist:
        ask_state = get_manual_ask_state(claimed.pk, claimed.manual_claimed_at)
        # The window never shows who asked: no member names on the runner socket.
        questions = tuple(
            replace(view.to_payload(), asked_by="") for view in ask_state.questions
        )
        can_ask = ask_state.can_ask
    return ManualActiveCasePayload(
        pivot_id=claimed.pk,
        claim_token=claimed.manual_claim_token,
        title=claimed.test_case.title[:MANUAL_CASE_TITLE_MAX_CHARS],
        preconditions=claimed.test_case.preconditions.strip()[
            :MANUAL_PRECONDITIONS_MAX_CHARS
        ],
        steps=tuple(
            ManualStepPayload(
                num=step.num,
                text=step.text[:MANUAL_STEP_TEXT_MAX_CHARS],
                expected=step.expected[:MANUAL_STEP_TEXT_MAX_CHARS],
            )
            for step in steps
        ),
        claimed_at=(
            claimed.manual_claimed_at.isoformat()
            if claimed.manual_claimed_at is not None
            else ""
        ),
        questions=questions,
        can_ask=can_ask,
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
