"""Use case: the state of a project's machine setup chat, for the page and socket."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from machine_setup.exceptions import (
    SetupMachineNotReadyError,
    SetupRunActiveError,
    SetupTurnActiveError,
)
from machine_setup.models import (
    LIVE_TURN_STATUSES,
    SetupConversation,
    SetupMessage,
    SetupScreenshot,
    SetupTurn,
)
from machine_setup.payloads import (
    SetupMessagePayload,
    SetupTurnPayload,
    message_payload,
    turn_payload,
)
from machine_setup.turn_state import is_turn_stale
from projects.models import Project, TestRun, TestRunStatus
from projects.services import MachineState, get_machine_state

SETUP_PAGE_MESSAGE_LIMIT = 300


@dataclass(frozen=True)
class SetupPage:
    conversation_id: int | None
    messages: list[SetupMessagePayload]
    hidden_message_count: int
    turn: SetupTurnPayload | None
    machine_state: MachineState
    run_active: bool
    send_blockers: tuple[str, ...]
    can_send: bool
    max_iterations: int
    # A live turn belongs to a conversation archived by Reset.
    finishing_previous_turn: bool
    # That archived conversation, so the page ignores its late rows.
    finishing_conversation_id: int | None


def get_setup_page(project: Project, *, include_messages: bool = True) -> SetupPage:
    """About four queries; never loads ``llm_transcript``.

    The HTML page passes ``include_messages=False``: the socket's
    ``current_state`` delivers the rows, so they are built once per load.
    """
    conversation_id = (
        SetupConversation.objects.filter(project=project, archived_at__isnull=True)
        .values_list("id", flat=True)
        .first()
    )
    live_turn = (
        SetupTurn.objects.filter(project=project, status__in=LIVE_TURN_STATUSES)
        .order_by("-id")
        .first()
    )
    if live_turn is not None and is_turn_stale(live_turn, timezone.now()):
        # The next send or stop closes it as worker_lost; it blocks nothing.
        shown_turn: SetupTurn | None = live_turn
        live_turn = None
    else:
        shown_turn = live_turn
    if shown_turn is None and conversation_id is not None:
        shown_turn = (
            SetupTurn.objects.filter(conversation_id=conversation_id)
            .order_by("-id")
            .first()
        )

    messages, hidden_count = (
        _recent_messages(conversation_id) if include_messages else ([], 0)
    )
    machine_state = get_machine_state(project)
    run_active = TestRun.objects.filter(
        project=project, status=TestRunStatus.STARTED
    ).exists()

    finishing_conversation_id = (
        live_turn.conversation_id
        if live_turn is not None and live_turn.conversation_id != conversation_id
        else None
    )
    blockers: list[str] = []
    if machine_state != MachineState.READY:
        blockers.append(SetupMachineNotReadyError().message)
    if run_active:
        blockers.append(SetupRunActiveError().message)
    if live_turn is not None:
        blockers.append(SetupTurnActiveError().message)

    return SetupPage(
        conversation_id=conversation_id,
        messages=messages,
        hidden_message_count=hidden_count,
        turn=turn_payload(shown_turn) if shown_turn is not None else None,
        machine_state=machine_state,
        run_active=run_active,
        send_blockers=tuple(blockers),
        can_send=not blockers,
        max_iterations=settings.MACHINE_SETUP_MAX_ITERATIONS,
        finishing_previous_turn=finishing_conversation_id is not None,
        finishing_conversation_id=finishing_conversation_id,
    )


def _recent_messages(
    conversation_id: int | None,
) -> tuple[list[SetupMessagePayload], int]:
    """The last ``SETUP_PAGE_MESSAGE_LIMIT`` rows in id order, and how many are older."""
    if conversation_id is None:
        return [], 0
    rows = list(
        SetupMessage.objects.filter(conversation_id=conversation_id)
        .select_related("author")
        .prefetch_related(
            Prefetch("screenshots", queryset=SetupScreenshot.objects.order_by("id"))
        )
        .order_by("-id")[:SETUP_PAGE_MESSAGE_LIMIT]
    )
    rows.reverse()
    hidden_count = 0
    if len(rows) == SETUP_PAGE_MESSAGE_LIMIT:
        hidden_count = SetupMessage.objects.filter(
            conversation_id=conversation_id, id__lt=rows[0].pk
        ).count()
    return [message_payload(row) for row in rows], hidden_count
