"""Use case: the Machine setup page's Analysis section (read-only).

The project's active case analysis (``run_triage``) threads, listed apart
from the per-case threads, and, for writable members, the runs an analysis
can be started for. Two queries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from machine_setup.models import SetupConversation, SetupConversationKind, SetupTurn
from projects.models import Project, TestRun, TestRunStatus
from run_triage.turn_liveness import live_turn_q

ANALYSIS_THREAD_LIMIT = 20
ANALYSIS_START_RUN_LIMIT = 30
DELETED_RUN_NAME = "Deleted run"

# The run list's labels.
RUN_STATUS_LABELS: dict[str, str] = {
    TestRunStatus.WAITING: "Draft",
    TestRunStatus.STARTED: "Running",
    TestRunStatus.PAUSED: "Needs help",
    TestRunStatus.DONE: "Done",
    TestRunStatus.CANCELLED: "Cancelled",
}
_STATE_LABELS = {"running": "Running", "ready": "Ready"}


@dataclass(frozen=True)
class AnalysisThread:
    conversation_id: int
    # None once the run was deleted (the thread is read-only).
    test_run_id: int | None
    run_name: str
    run_status: str
    run_status_label: str
    # "running" while the thread's latest turn is live, else "ready".
    state: str
    state_label: str
    created_at: datetime


@dataclass(frozen=True)
class StartableRun:
    test_run_id: int
    run_name: str
    run_status_label: str


@dataclass(frozen=True)
class AnalysisSection:
    # Newest run first.
    threads: list[AnalysisThread]
    # Non-draft runs without an active analysis thread, newest first; empty
    # for read-only members.
    startable_runs: list[StartableRun]
    # A list hit its limit: older runs open their analysis from the run page.
    older_run_hint: bool


def run_status_label(status: str) -> str:
    return RUN_STATUS_LABELS.get(status, status)


def get_analysis_section(
    project: Project, *, include_startable: bool
) -> AnalysisSection:
    """The section's threads and, when ``include_startable``, its startable runs."""
    threads, threads_capped = _analysis_threads(project)
    startable: list[StartableRun] = []
    runs_capped = False
    if include_startable:
        startable, runs_capped = _startable_runs(project)
    return AnalysisSection(
        threads=threads,
        startable_runs=startable,
        older_run_hint=threads_capped or runs_capped,
    )


def _analysis_threads(project: Project) -> tuple[list[AnalysisThread], bool]:
    # Same "live" rule (stale cutoff included) as the run page's analysis card.
    turn_live = Exists(
        SetupTurn.objects.filter(conversation=OuterRef("pk")).filter(
            live_turn_q(timezone.now())
        )
    )
    rows = list(
        SetupConversation.objects.filter(
            project=project,
            kind=SetupConversationKind.RUN_TRIAGE,
            archived_at__isnull=True,
        )
        .select_related("triage_run")
        .only(
            "id",
            "created_at",
            "triage_run__id",
            "triage_run__name",
            "triage_run__status",
        )
        .annotate(turn_live=turn_live)
        .order_by(F("triage_run_id").desc(nulls_last=True), "-id")[
            : ANALYSIS_THREAD_LIMIT + 1
        ]
    )
    threads: list[AnalysisThread] = []
    for row in rows[:ANALYSIS_THREAD_LIMIT]:
        state = "running" if getattr(row, "turn_live", False) else "ready"
        run = row.triage_run
        threads.append(
            AnalysisThread(
                conversation_id=row.pk,
                test_run_id=run.pk if run is not None else None,
                run_name=run.display_name if run is not None else DELETED_RUN_NAME,
                run_status=run.status if run is not None else "",
                run_status_label=(
                    run_status_label(run.status) if run is not None else ""
                ),
                state=state,
                state_label=_STATE_LABELS[state],
                created_at=row.created_at,
            )
        )
    return threads, len(rows) > ANALYSIS_THREAD_LIMIT


def _startable_runs(project: Project) -> tuple[list[StartableRun], bool]:
    active_thread = SetupConversation.objects.filter(
        triage_run=OuterRef("pk"), archived_at__isnull=True
    )
    runs = list(
        TestRun.objects.filter(project=project)
        .exclude(status=TestRunStatus.WAITING)
        .filter(~Exists(active_thread))
        .only("id", "name", "status")
        .order_by("-id")[: ANALYSIS_START_RUN_LIMIT + 1]
    )
    return [
        StartableRun(
            test_run_id=run.pk,
            run_name=run.display_name,
            run_status_label=run_status_label(run.status),
        )
        for run in runs[:ANALYSIS_START_RUN_LIMIT]
    ], len(runs) > ANALYSIS_START_RUN_LIMIT
