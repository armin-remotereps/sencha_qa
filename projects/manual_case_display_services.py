"""Read-only display data of a Manual run for the run page and the case page.

The web never performs a manual case (the Test Runner window does, D5); these
panels only show who holds a case, what the tester reported, the upload of
the screenshots and the AI rewrite for TestRail. Every text the runner sent is
returned as plain strings for autoescaped templates (never ``|safe``).

Queries: the run panel costs at most two (the claimed case, the page's open
uploads); the case panel at most three (the current hand-in, the runner's
progress report of a claimed case, the case's manual discussion thread) and
reuses the case page's prefetched screenshots.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal

from django.utils import timezone

from agents.checkpoint import checkpoint_from_json, final_step_indices
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.turn_state import live_turn_exists
from projects.manual_case_pause_services import (
    ManualPausePanel,
    get_manual_pause_panel,
)
from projects.manual_case_progress_services import (
    ManualProgressView,
    load_manual_progress,
)
from projects.manual_refinement_services import refined_text_is_current
from projects.manual_run_policy import (
    MANUAL_DISCUSS_NOT_CLAIMED,
    MANUAL_PAUSE_AGENT_BUSY,
    MANUAL_TOOL_NAMES,
    is_claimed_manual_case,
    is_manual_checkpoint,
    is_manual_run,
    runner_supports_manual,
)
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    Project,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

RUNNER_OFFLINE_NOTICE: Final = (
    "The Test Runner is not connected. Connect it to continue this manual run."
)
RUNNER_OUTDATED_NOTICE: Final = (
    "Update the Test Runner (and enable its window) to continue this manual run."
)

_SHOT_LABELS: Final[Mapping[str, str]] = {
    "manual_pass": "Pass",
    "manual_fail": "Fail",
    "manual_case": "Case",
}

_OPEN_UPLOAD_STATES: Final = (
    ManualUploadState.UPLOADING,
    ManualUploadState.INCOMPLETE,
)

StepOutcome = Literal["passed", "failed", "not_run"]


@dataclass(frozen=True)
class ManualUpload:
    """Where a hand-in's screenshots are."""

    state: str
    label: str
    received: int
    expected: int

    @property
    def is_uploading(self) -> bool:
        return self.state == ManualUploadState.UPLOADING

    @property
    def is_incomplete(self) -> bool:
        return self.state == ManualUploadState.INCOMPLETE

    @property
    def is_complete(self) -> bool:
        return self.state == ManualUploadState.COMPLETE


@dataclass(frozen=True)
class ManualRunPanel:
    """Run page: "Manual run: performed at the Test Runner"."""

    is_live: bool
    claimed_pivot_id: int | None
    claimed_title: str
    claimed_by_label: str
    claimed_at: datetime | None
    # "" when the runner is connected and supports manual runs (or the run is
    # not Running).
    runner_notice: str
    # Pivot id -> its open upload (uploading / incomplete), cases on this page.
    uploads: Mapping[int, ManualUpload]


@dataclass(frozen=True)
class ManualShot:
    url: str
    label: str


@dataclass(frozen=True)
class ManualStepRow:
    num: int
    description: str
    expected_result: str
    outcome: StepOutcome
    outcome_label: str
    # The tester's note, verbatim ("" for a Pass without one).
    note: str
    # The AI rewrite for TestRail ("" when none).
    refined_note: str
    duration_seconds: int | None
    shots: tuple[ManualShot, ...]


@dataclass(frozen=True)
class ManualCasePanel:
    """Case page: the manual hand-in of this case."""

    run_is_manual: bool
    # Claim (the case is being performed at the Test Runner now).
    is_claimed: bool
    claimed_by_label: str
    claimed_at: datetime | None
    can_release: bool
    # Current hand-in ("" / None before one).
    has_submission: bool
    verdict: str
    verdict_label: str
    verdict_note: str
    submitted_label: str
    submitted_at: datetime | None
    upload: ManualUpload | None
    # AI rewrite for TestRail.
    refine_state: str
    refine_state_label: str
    is_refined: bool
    refined_summary: str
    steps: tuple[ManualStepRow, ...]
    case_shots: tuple[ManualShot, ...]
    # Pause and discuss (None unless the run is Manual).
    pause: ManualPausePanel | None = None
    # The Test Runner's latest marks of the claimed case (advisory; None
    # without a claim or a report for the current claim).
    progress: ManualProgressView | None = None
    # The case's open manual discussion thread (None before one).
    discussion_thread_id: int | None = None
    # A new discussion can be opened (Manual run Running, case claimed and in
    # progress); else discuss_blocker says why ("" when it can).
    can_discuss: bool = False
    discuss_blocker: str = ""

    @property
    def has_refined_step_notes(self) -> bool:
        return any(row.refined_note for row in self.steps)


def _upload_of(submission: ManualCaseSubmission) -> ManualUpload:
    return ManualUpload(
        state=submission.upload_state,
        label=submission.get_upload_state_display(),
        received=submission.shots_received,
        expected=submission.shots_expected,
    )


def _runner_notice(project: Project, test_run: TestRun) -> str:
    """Why the tester cannot go on at the Test Runner (no query)."""
    if test_run.status != TestRunStatus.STARTED:
        return ""
    if not project.agent_connected:
        return RUNNER_OFFLINE_NOTICE
    capabilities = project.agent_capabilities
    if not isinstance(capabilities, list) or not runner_supports_manual(
        str(item) for item in capabilities
    ):
        return RUNNER_OUTDATED_NOTICE
    return ""


def get_manual_run_panel(
    *, project: Project, test_run: TestRun, pivot_ids: Iterable[int]
) -> ManualRunPanel | None:
    """The run page's manual panel; None unless the run is in Manual mode.

    ``pivot_ids``: the cases shown on this page (their open uploads are
    listed). At most two queries.
    """
    if not is_manual_run(test_run):
        return None
    claimed = (
        TestRunTestCase.objects.filter(test_run=test_run)
        .exclude(manual_claim_token="")
        .select_related("test_case")
        .only(
            "id",
            "manual_claimed_label",
            "manual_claimed_at",
            "test_case__title",
        )
        .first()
    )
    ids = list(pivot_ids)
    uploads: dict[int, ManualUpload] = {}
    if ids:
        rows = (
            ManualCaseSubmission.objects.filter(
                test_run_test_case_id__in=ids,
                upload_state__in=_OPEN_UPLOAD_STATES,
            )
            .only(
                "test_run_test_case_id",
                "upload_state",
                "shots_received",
                "shots_expected",
                "submitted_at",
            )
            .order_by("submitted_at")
        )
        # Ascending: a newer hand-in of the same case replaces an older one.
        for row in rows:
            uploads[row.test_run_test_case_id] = _upload_of(row)
    return ManualRunPanel(
        is_live=test_run.status == TestRunStatus.STARTED,
        claimed_pivot_id=claimed.id if claimed is not None else None,
        claimed_title=claimed.test_case.title if claimed is not None else "",
        claimed_by_label=claimed.manual_claimed_label if claimed is not None else "",
        claimed_at=claimed.manual_claimed_at if claimed is not None else None,
        runner_notice=_runner_notice(project, test_run),
        uploads=uploads,
    )


def _current_submission(pivot: TestRunTestCase) -> ManualCaseSubmission | None:
    return (
        ManualCaseSubmission.objects.filter(test_run_test_case=pivot)
        .exclude(upload_state=ManualUploadState.SUPERSEDED)
        .order_by("-submitted_at")
        .first()
    )


def _group_shots(
    screenshots: Sequence[TestRunScreenshot],
) -> tuple[dict[int, list[ManualShot]], list[ManualShot]]:
    by_step: dict[int, list[ManualShot]] = {}
    case_shots: list[ManualShot] = []
    for shot in screenshots:
        if shot.tool_name not in MANUAL_TOOL_NAMES:
            continue
        item = ManualShot(
            url=shot.image.url, label=_SHOT_LABELS.get(shot.tool_name, "")
        )
        if shot.step_num is None or shot.tool_name == "manual_case":
            case_shots.append(item)
        else:
            by_step.setdefault(shot.step_num, []).append(item)
    return by_step, case_shots


def _refined_step_notes(
    submission: ManualCaseSubmission | None, is_refined: bool
) -> Mapping[int, str]:
    if submission is None or not is_refined:
        return {}
    raw = submission.refined_step_notes
    if not isinstance(raw, dict):
        return {}
    notes: dict[int, str] = {}
    for key, value in raw.items():
        if isinstance(value, str) and str(key).isdigit():
            notes[int(key)] = value
    return notes


def _step_rows(
    pivot: TestRunTestCase,
    screenshots: Sequence[TestRunScreenshot],
    refined: Mapping[int, str],
) -> tuple[tuple[ManualStepRow, ...], tuple[ManualShot, ...]]:
    by_step, case_shots = _group_shots(screenshots)
    checkpoint = checkpoint_from_json(pivot.execution_checkpoint)
    if checkpoint is None:
        return (), tuple(case_shots)
    finals = final_step_indices(checkpoint.attempts)
    rows: list[ManualStepRow] = []
    for num, task in enumerate(checkpoint.plan, start=1):
        index = finals.get(num)
        attempt = checkpoint.attempts[index] if index is not None else None
        outcome: StepOutcome
        if attempt is None:
            outcome, label, note, duration = "not_run", "Not run", "", None
        elif attempt.result.status == "pass":
            outcome, label = "passed", "Passed"
            note, duration = attempt.result.summary, attempt.duration_seconds
        else:
            outcome, label = "failed", "Failed"
            note, duration = attempt.result.summary, attempt.duration_seconds
        rows.append(
            ManualStepRow(
                num=num,
                description=task.description,
                expected_result=task.expected_result,
                outcome=outcome,
                outcome_label=label,
                note=note,
                refined_note=refined.get(num, ""),
                duration_seconds=duration,
                shots=tuple(by_step.get(num, ())),
            )
        )
    return tuple(rows), tuple(case_shots)


def _pause_panel(pivot: TestRunTestCase) -> ManualPausePanel:
    """The case's pause panel; Continue off while any Machine setup turn of
    the project is live (one query, only when Continue would apply)."""
    panel = get_manual_pause_panel(pivot.test_run, pivot)
    if panel.can_continue and live_turn_exists(
        pivot.test_run.project_id, timezone.now()
    ):
        return replace(panel, can_continue=False, blocker=MANUAL_PAUSE_AGENT_BUSY)
    return panel


def get_manual_case_panel(
    *,
    pivot: TestRunTestCase,
    screenshots: Sequence[TestRunScreenshot],
) -> ManualCasePanel | None:
    """The case page's manual card; None unless the case is (or was) manual.

    ``pivot`` has ``test_run`` and ``test_case`` loaded; ``screenshots`` are
    the case page's prefetched rows. At most four queries (the fourth only
    while the case is paused).
    """
    run_is_manual = is_manual_run(pivot.test_run)
    if not run_is_manual and not is_manual_checkpoint(pivot.execution_checkpoint):
        return None
    submission = _current_submission(pivot)
    # Shown only while the rewrite still matches the case as it is now.
    is_refined = submission is not None and refined_text_is_current(submission, pivot)
    refined = _refined_step_notes(submission, is_refined)
    steps, case_shots = _step_rows(pivot, screenshots, refined)
    is_claimed = bool(pivot.manual_claim_token)
    can_discuss = is_claimed_manual_case(pivot.test_run, pivot)
    pause = _pause_panel(pivot) if run_is_manual else None
    discussion_thread_id = (
        SetupConversation.objects.filter(
            manual_case_id=pivot.pk,
            kind=SetupConversationKind.MANUAL_CASE,
            archived_at__isnull=True,
        )
        .values_list("id", flat=True)
        .first()
    )
    return ManualCasePanel(
        run_is_manual=run_is_manual,
        is_claimed=is_claimed,
        claimed_by_label=pivot.manual_claimed_label,
        claimed_at=pivot.manual_claimed_at,
        can_release=(is_claimed and pivot.status == TestRunTestCaseStatus.IN_PROGRESS),
        has_submission=submission is not None,
        verdict=submission.verdict if submission is not None else "",
        verdict_label=(
            submission.get_verdict_display() if submission is not None else ""
        ),
        verdict_note=submission.verdict_note if submission is not None else "",
        submitted_label=submission.submitted_label if submission is not None else "",
        submitted_at=submission.submitted_at if submission is not None else None,
        upload=_upload_of(submission) if submission is not None else None,
        refine_state=submission.refine_state if submission is not None else "",
        refine_state_label=(
            submission.get_refine_state_display() if submission is not None else ""
        ),
        is_refined=is_refined,
        refined_summary=(
            submission.refined_summary if submission is not None and is_refined else ""
        ),
        steps=steps,
        case_shots=case_shots,
        pause=pause,
        # Fenced by the claim token: no report outlives its claim.
        progress=load_manual_progress(pivot) if is_claimed else None,
        discussion_thread_id=discussion_thread_id,
        can_discuss=can_discuss,
        discuss_blocker="" if can_discuss else MANUAL_DISCUSS_NOT_CLAIMED,
    )
