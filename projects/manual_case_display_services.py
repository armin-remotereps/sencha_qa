"""Read-only display data of a Manual run for the run page and the case page.

The web never performs a manual case (the Test Runner window does, D5); these
panels only show who holds a case, what the tester reported, the upload of
the screenshots and the AI rewrite for TestRail. Every text the runner sent is
returned as plain strings for autoescaped templates (never ``|safe``).

Queries: the run panel costs at most two (the claimed case, the page's open
uploads); the case panel at most three (the current hand-in, the Ask AI
questions, the steps' "How to do it" guide) and reuses the case page's
prefetched screenshots.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from agents.checkpoint import checkpoint_from_json, final_step_indices
from projects.how_to_view import PersonHowToView, how_to_view
from projects.manual_refinement_services import refined_text_is_current
from projects.manual_run_policy import (
    MANUAL_TOOL_NAMES,
    is_manual_checkpoint,
    is_manual_run,
    runner_supports_manual,
)
from projects.manual_step_guide_read import (
    ManualGuideView,
    ManualStepHowToState,
    load_manual_guide,
)
from projects.models import (
    ManualCaseSubmission,
    ManualUploadState,
    PersonStepQuestion,
    Project,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)

# Ask AI questions listed on the case page (newest kept, shown oldest first).
_QUESTIONS_SHOWN: Final = 20

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
    # The AI's "How to do it" for the step (read only; () without a guide).
    how_to: tuple[PersonHowToView, ...] = ()
    # "ready" with items, "pending" while being written, else "none".
    how_to_state: ManualStepHowToState = "none"

    @property
    def how_to_has_sources(self) -> bool:
        return any(item.source_label for item in self.how_to)


@dataclass(frozen=True)
class ManualQuestionRow:
    question_id: int
    step_label: str
    question: str
    answer: str
    status: str
    status_label: str
    created_at: datetime


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
    questions: tuple[ManualQuestionRow, ...]
    # The shell the steps' suggested commands are for ("" without a guide).
    how_to_shell_label: str = ""
    # Some step's how-to is still being written ("reload to see it").
    guide_preparing: bool = False

    def questions_payload(self) -> list[dict[str, object]]:
        """Seed of the card's live Ask AI list (``json_script``); the case
        socket's ``person_step_questions`` event (``person_step_id`` 0)
        replaces it and carries no step number, so the step label is kept
        per ``question_id``."""
        return [
            {
                "question_id": row.question_id,
                "step_label": row.step_label,
                "status": row.status,
                "question": row.question,
                "answer": row.answer,
                "asked_by": "",
            }
            for row in self.questions
        ]

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


def _questions(pivot: TestRunTestCase) -> tuple[ManualQuestionRow, ...]:
    rows = list(
        PersonStepQuestion.objects.filter(manual_case=pivot)
        .only(
            "manual_step_num",
            "question",
            "answer",
            "status",
            "created_at",
        )
        .order_by("-created_at")[:_QUESTIONS_SHOWN]
    )
    rows.reverse()
    return tuple(
        ManualQuestionRow(
            question_id=row.pk,
            step_label=(
                f"Step {row.manual_step_num}"
                if row.manual_step_num is not None
                else "The whole case"
            ),
            question=row.question,
            answer=row.answer,
            status=row.status,
            status_label=row.get_status_display(),
            created_at=row.created_at,
        )
        for row in rows
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
    guide: ManualGuideView | None,
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
        step_guide = guide.step(num) if guide is not None else None
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
                how_to=(
                    tuple(
                        how_to_view(item, pivot.test_run.project_id)
                        for item in step_guide.how_to
                    )
                    if step_guide is not None
                    else ()
                ),
                how_to_state=step_guide.state if step_guide is not None else "none",
            )
        )
    return tuple(rows), tuple(case_shots)


def get_manual_case_panel(
    *,
    pivot: TestRunTestCase,
    screenshots: Sequence[TestRunScreenshot],
) -> ManualCasePanel | None:
    """The case page's manual card; None unless the case is (or was) manual.

    ``pivot`` has ``test_run`` and ``test_case`` loaded; ``screenshots`` are
    the case page's prefetched rows. At most three queries.
    """
    run_is_manual = is_manual_run(pivot.test_run)
    if not run_is_manual and not is_manual_checkpoint(pivot.execution_checkpoint):
        return None
    submission = _current_submission(pivot)
    # Shown only while the rewrite still matches the case as it is now.
    is_refined = submission is not None and refined_text_is_current(submission, pivot)
    refined = _refined_step_notes(submission, is_refined)
    # Shown claimed or handed in, while it still matches the case (Q5); no
    # query before a claim wrote the verbatim steps.
    guide = (
        load_manual_guide(pivot)
        if is_manual_checkpoint(pivot.execution_checkpoint)
        else None
    )
    steps, case_shots = _step_rows(pivot, screenshots, refined, guide)
    is_claimed = bool(pivot.manual_claim_token)
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
        questions=_questions(pivot),
        how_to_shell_label=guide.shell_label if guide is not None else "",
        guide_preparing=guide is not None and guide.preparing,
    )
