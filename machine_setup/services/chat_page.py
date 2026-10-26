"""Use case: the state of a project's machine setup chat, for the page and socket."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db.models import (
    Count,
    F,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Window,
)
from django.db.models.functions import RowNumber
from django.utils import timezone

from machine_setup.exceptions import (
    SetupMachineNotReadyError,
    SetupRunActiveError,
    SetupTurnActiveError,
)
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupConversation,
    SetupConversationKind,
    SetupDocument,
    SetupFixResult,
    SetupMessage,
    SetupMessageKind,
    SetupScreenshot,
    SetupTurn,
)
from machine_setup.payloads import (
    SetupMessagePayload,
    SetupTurnPayload,
    message_payload,
    turn_payload,
)
from machine_setup.subtask_types import RUN_SETUP_TASK_TOOL
from machine_setup.turn_state import is_turn_stale
from projects.case_help_services import HelpPanel, get_help_panel
from projects.models import (
    HelpAutoFixState,
    HelpResolution,
    Project,
    TestRun,
    TestRunStatus,
)
from projects.services import MachineState, get_machine_state
from visual_engine.services.pool_status import get_visual_engine_status
from visual_engine.types import VisualEngineStatusPayload

SETUP_PAGE_MESSAGE_LIMIT = 300
# Newest children loaded per run_setup_task row on page load.
SUBTASK_PAGE_CHILD_LIMIT = 50
# Newest help threads (auto-fix and help chat) listed on the page.
HELP_THREAD_LIMIT = 20

# Thread kinds that belong to a help request.
HELP_THREAD_KINDS = (SetupConversationKind.AUTO_FIX, SetupConversationKind.CASE_HELP)

# Send blocker while a turn is live in a thread other than the one shown.
OTHER_THREAD_TURN_MESSAGE = (
    "The setup agent is working in another thread. Wait for it to finish or "
    "stop it there."
)


@dataclass(frozen=True)
class HelpThread:
    """A help thread (auto-fix or help chat) as listed and shown in its header."""

    conversation_id: int
    # "auto_fix" or "case_help", and its label ("Auto-fix" / "Help chat").
    kind: str
    kind_label: str
    help_request_id: int | None
    created_at: datetime
    # The help request's case; None when the request row is gone.
    test_run_id: int | None
    pivot_id: int | None
    case_title: str
    step_num: int | None
    # Auto-fix: "running", "fixed" or "not_fixed". Help chat: "running",
    # "open" or "resolved".
    state: str
    state_label: str


_THREAD_STATE_LABELS = {
    "running": "Running",
    "fixed": "Fixed",
    "not_fixed": "Not fixed",
    "open": "Needs help",
}


@dataclass(frozen=True)
class SetupPage:
    conversation_id: int | None
    # The shown thread's kind: "chat" (the main chat), "auto_fix" or
    # "case_help".
    kind: str
    # Set when the shown thread is a help thread (auto-fix or help chat).
    thread: HelpThread | None
    messages: list[SetupMessagePayload]
    hidden_message_count: int
    turn: SetupTurnPayload | None
    machine_state: MachineState
    visual_engine: VisualEngineStatusPayload
    run_active: bool
    send_blockers: tuple[str, ...]
    can_send: bool
    max_iterations: int
    # A live turn belongs to a conversation archived by Reset.
    finishing_previous_turn: bool
    # That archived conversation, so the page ignores its late rows.
    finishing_conversation_id: int | None
    # A turn is live in another (not archived) thread of the project.
    other_thread_turn_live: bool


def find_thread(project: Project, conversation_id: int) -> SetupConversation | None:
    """A non-archived thread (main chat or help thread) of this project, or None."""
    return (
        SetupConversation.objects.filter(
            pk=conversation_id, project=project, archived_at__isnull=True
        )
        .defer("llm_transcript")
        .first()
    )


def list_help_threads(
    project: Project, limit: int = HELP_THREAD_LIMIT
) -> list[HelpThread]:
    """The newest help threads of the project, newest first; one query."""
    rows = _help_threads(project).order_by("-id")[:limit]
    return [_thread_from_row(row) for row in rows]


def _help_threads(project: Project) -> QuerySet[SetupConversation]:
    latest_turn_status = (
        SetupTurn.objects.filter(conversation=OuterRef("pk"))
        .order_by("-id")
        .values("status")[:1]
    )
    return (
        SetupConversation.objects.filter(
            project=project,
            kind__in=HELP_THREAD_KINDS,
            archived_at__isnull=True,
        )
        .select_related("help_request__test_run_test_case__test_case")
        .only(
            "id",
            "kind",
            "created_at",
            "fix_result",
            "help_request__id",
            "help_request__step_num",
            "help_request__auto_fix_state",
            "help_request__resolution",
            "help_request__test_run_test_case__id",
            "help_request__test_run_test_case__test_run",
            "help_request__test_run_test_case__test_case__id",
            "help_request__test_run_test_case__test_case__title",
        )
        .annotate(latest_turn_status=Subquery(latest_turn_status))
    )


def _thread_from_row(row: SetupConversation) -> HelpThread:
    request = row.help_request
    latest_status: str | None = getattr(row, "latest_turn_status", None)
    state_label = ""
    if latest_status in LIVE_TURN_STATUSES:
        state = "running"
    elif row.kind == SetupConversationKind.CASE_HELP:
        if request is None or request.resolution != HelpResolution.OPEN:
            state = "resolved"
            state_label = (
                request.get_resolution_display() if request is not None else "Closed"
            )
        else:
            state = "open"
    elif request is not None:
        state = (
            "fixed" if request.auto_fix_state == HelpAutoFixState.FIXED else "not_fixed"
        )
    else:
        state = "fixed" if row.fix_result == SetupFixResult.FIXED else "not_fixed"
    pivot = request.test_run_test_case if request is not None else None
    return HelpThread(
        conversation_id=row.pk,
        kind=row.kind,
        kind_label=row.get_kind_display(),
        help_request_id=request.pk if request is not None else None,
        created_at=row.created_at,
        test_run_id=pivot.test_run_id if pivot is not None else None,
        pivot_id=pivot.pk if pivot is not None else None,
        case_title=pivot.test_case.title if pivot is not None else "",
        step_num=request.step_num if request is not None else None,
        state=state,
        state_label=state_label or _THREAD_STATE_LABELS[state],
    )


def get_thread_help_panel(project: Project, thread: HelpThread) -> HelpPanel | None:
    """The help panel of a help thread's run, when its run waits on this request.

    None when the run is not paused (no further queries), or when the run's
    latest help request is another one (the case paused again later).
    """
    if thread.test_run_id is None or thread.help_request_id is None:
        return None
    # Whole row: the panel's gates read several run fields.
    test_run = TestRun.objects.filter(pk=thread.test_run_id, project=project).first()
    if test_run is None:
        return None
    panel = get_help_panel(test_run, project, pivot_id=thread.pivot_id)
    if panel is None or panel.request.pk != thread.help_request_id:
        return None
    return panel


def get_setup_page(
    project: Project,
    conversation: SetupConversation | None = None,
    *,
    include_messages: bool = True,
) -> SetupPage:
    """The main chat, or ``conversation`` (a thread from ``find_thread``).

    About five queries; never loads ``llm_transcript``. The HTML page passes
    ``include_messages=False``: the socket's ``current_state`` delivers the
    rows, so they are built once per load.
    """
    thread: HelpThread | None = None
    if conversation is None:
        kind: str = SetupConversationKind.CHAT
        conversation_id = (
            SetupConversation.objects.filter(
                project=project,
                kind=SetupConversationKind.CHAT,
                archived_at__isnull=True,
            )
            .values_list("id", flat=True)
            .first()
        )
    else:
        kind = conversation.kind
        conversation_id = conversation.pk
        if kind in HELP_THREAD_KINDS:
            row = _help_threads(project).filter(pk=conversation_id).first()
            thread = _thread_from_row(row) if row is not None else None
    live_turn = (
        SetupTurn.objects.filter(project=project, status__in=LIVE_TURN_STATUSES)
        .order_by("-id")
        .first()
    )
    if live_turn is not None and is_turn_stale(live_turn, timezone.now()):
        # The next send, stop or page load closes it as worker_lost; it blocks
        # nothing.
        stale_turn: SetupTurn | None = live_turn
        live_turn = None
    else:
        stale_turn = None

    finishing_conversation_id: int | None = None
    other_thread_turn_live = False
    if live_turn is not None and live_turn.conversation_id != conversation_id:
        archived = SetupConversation.objects.filter(
            pk=live_turn.conversation_id, archived_at__isnull=False
        ).exists()
        if archived:
            finishing_conversation_id = live_turn.conversation_id
        else:
            other_thread_turn_live = True

    shown_turn: SetupTurn | None = None
    for candidate in (live_turn, stale_turn):
        if candidate is None:
            continue
        if candidate.conversation_id == conversation_id or (
            # The main chat keeps showing a turn Reset is still finishing.
            conversation is None
            and candidate.conversation_id == finishing_conversation_id
        ):
            shown_turn = candidate
    if shown_turn is None and conversation_id is not None:
        shown_turn = (
            SetupTurn.objects.filter(conversation_id=conversation_id)
            .order_by("-id")
            .first()
        )

    messages, hidden_count = (
        _recent_messages(conversation_id) if include_messages else ([], 0)
    )
    visual_engine = get_visual_engine_status()
    machine_state = get_machine_state(project, visual_engine)
    run_active = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).exists()

    blockers: list[str] = []
    if machine_state != MachineState.READY:
        blockers.append(SetupMachineNotReadyError().message)
    if run_active:
        blockers.append(SetupRunActiveError().message)
    if other_thread_turn_live:
        blockers.append(OTHER_THREAD_TURN_MESSAGE)
    elif live_turn is not None:
        blockers.append(SetupTurnActiveError().message)

    return SetupPage(
        conversation_id=conversation_id,
        kind=kind,
        thread=thread,
        messages=messages,
        hidden_message_count=hidden_count,
        turn=turn_payload(shown_turn) if shown_turn is not None else None,
        machine_state=machine_state,
        visual_engine=visual_engine.to_payload(),
        run_active=run_active,
        send_blockers=tuple(blockers),
        can_send=not blockers,
        max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
        finishing_previous_turn=finishing_conversation_id is not None,
        finishing_conversation_id=finishing_conversation_id,
        other_thread_turn_live=other_thread_turn_live,
    )


def _recent_messages(
    conversation_id: int | None,
) -> tuple[list[SetupMessagePayload], int]:
    """The last ``SETUP_PAGE_MESSAGE_LIMIT`` top-level rows, and how many are older.

    Each ``run_setup_task`` row in that window brings its newest
    ``SUBTASK_PAGE_CHILD_LIMIT`` children; the rest are counted on the row as
    ``hidden_child_count``. One flat list in id order, in five or six queries.
    """
    if conversation_id is None:
        return [], 0
    screenshots = Prefetch(
        "screenshots", queryset=SetupScreenshot.objects.order_by("id")
    )
    # The payload reads documents of USER rows and the proposal of card rows;
    # case_ids can be large and is never shown.
    documents = Prefetch(
        "documents",
        queryset=SetupDocument.objects.only(
            "id", "message_id", "file_name", "size_bytes", "char_count"
        ),
    )
    rows = list(
        SetupMessage.objects.filter(
            conversation_id=conversation_id, parent__isnull=True
        )
        .select_related("author", "run_proposal")
        .defer("run_proposal__case_ids")
        .prefetch_related(screenshots, documents)
        .order_by("-id")[:SETUP_PAGE_MESSAGE_LIMIT]
    )
    rows.reverse()
    hidden_count = 0
    if len(rows) == SETUP_PAGE_MESSAGE_LIMIT:
        hidden_count = SetupMessage.objects.filter(
            conversation_id=conversation_id, parent__isnull=True, id__lt=rows[0].pk
        ).count()

    delegation_ids = [row.pk for row in rows if row.tool_name == RUN_SETUP_TASK_TOOL]
    children: list[SetupMessage] = []
    child_totals: dict[int, int] = {}
    loaded: dict[int, int] = {}
    if delegation_ids:
        annotated = list(
            SetupMessage.objects.filter(parent_id__in=delegation_ids)
            .annotate(
                row_number=Window(
                    RowNumber(),
                    partition_by=[F("parent_id")],
                    order_by=F("id").desc(),
                ),
                # Steps are tool calls; narration rows are not counted.
                tool_total=Window(
                    Count("id", filter=Q(kind=SetupMessageKind.TOOL)),
                    partition_by=[F("parent_id")],
                ),
            )
            .filter(row_number__lte=SUBTASK_PAGE_CHILD_LIMIT)
            .prefetch_related(screenshots)
            .order_by("id")
        )
        for child in annotated:
            if child.parent_id is not None:
                child_totals[child.parent_id] = child.tool_total
                if child.kind == SetupMessageKind.TOOL:
                    loaded[child.parent_id] = loaded.get(child.parent_id, 0) + 1
        children.extend(annotated)
    ordered = sorted([*rows, *children], key=lambda row: row.pk)
    return [
        message_payload(
            row,
            hidden_child_count=child_totals.get(row.pk, 0) - loaded.get(row.pk, 0),
        )
        for row in ordered
    ], hidden_count
