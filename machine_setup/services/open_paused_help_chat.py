"""Use case: open the discussion of a help request a person's pause opened.

Runs in the ``open_paused_help_chat`` task, enqueued by name on commit of the
request (``projects.case_help_state.enqueue_open_paused_help_chat``). The
request's thread is opened (or reused) and the pauser's first message is sent
as their own turn, exactly as if they had pressed "Open help chat" with it.

Idempotent: only a request whose ``pause_message_state`` is ``pending`` is
handled, the project row lock serialises a second delivery, and the state is
written with a conditional UPDATE. The message text is never logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.urls import reverse

from accounts.models import CustomUser
from machine_setup.exceptions import HelpChatSendRefusedError, MachineSetupError
from machine_setup.models import SetupConversation
from machine_setup.services.help_thread_state import broadcast_help_thread
from machine_setup.services.open_help_chat import open_help_chat
from projects.exceptions import RunHelpError
from projects.models import (
    PAUSE_MESSAGE_NOTE_MAX_LENGTH,
    CaseHelpRequest,
    HelpResolution,
    PauseMessageState,
    Project,
    TestRunTestCase,
)
from projects.project_access_services import get_project_access
from projects.services import broadcast_case_status

logger = logging.getLogger(__name__)

NO_MEMBER_NOTE = "Open the discussion on the web to send your message."
INTERNAL_ERROR_NOTE = "The discussion could not be opened. Open it yourself."


@dataclass(frozen=True)
class _Outcome:
    pivot: TestRunTestCase
    state: PauseMessageState
    # The request's thread, when one exists (sent, or refused with a thread).
    conversation_id: int | None


def open_paused_help_chat(help_request_id: int) -> None:
    """Open the paused case's discussion and send the pauser's message.

    Does nothing unless the request is open and its message state is
    ``pending``. Never raises for an expected refusal: the state records it
    (``sent``, ``refused`` with a user-safe note, or ``dropped``).
    """
    try:
        outcome = _open_locked(help_request_id)
    except Exception:
        logger.exception("Paused help request %s: discussion failed", help_request_id)
        # The work above rolled back; the person opens it from the page.
        _set_state(help_request_id, PauseMessageState.REFUSED, note=INTERNAL_ERROR_NOTE)
        return
    if outcome is None:
        return
    logger.info("Paused help request %s: discussion %s", help_request_id, outcome.state)
    _broadcast(help_request_id, outcome)


@transaction.atomic
def _open_locked(help_request_id: int) -> _Outcome | None:
    """The new message state, or None when there was nothing to do."""
    request = (
        CaseHelpRequest.objects.select_related(
            "test_run_test_case__test_run", "paused_by"
        )
        .defer("test_run_test_case__logs", "test_run_test_case__execution_checkpoint")
        .filter(pk=help_request_id)
        .first()
    )
    if request is None:
        return None
    pivot = request.test_run_test_case
    # Lock order of every run write (and of open_help_chat): the project row
    # first. It also serialises a second delivery of this task.
    project = Project.objects.select_for_update().get(pk=pivot.test_run.project_id)
    current = (
        CaseHelpRequest.objects.filter(pk=help_request_id)
        .values_list("resolution", "pause_message_state")
        .first()
    )
    if current is None:
        return None
    resolution, state = current
    if state != PauseMessageState.PENDING:
        return None
    if resolution != HelpResolution.OPEN:
        _set_state(help_request_id, PauseMessageState.DROPPED)
        return _Outcome(pivot, PauseMessageState.DROPPED, None)

    user = _writable_member(project.pk, request.paused_by)
    if user is None:
        _set_state(help_request_id, PauseMessageState.REFUSED, note=NO_MEMBER_NOTE)
        return _Outcome(pivot, PauseMessageState.REFUSED, _thread_id(help_request_id))

    try:
        thread = open_help_chat(
            project=project, pivot=pivot, user=user, text=request.pause_message
        )
    except HelpChatSendRefusedError as exc:
        _set_state(help_request_id, PauseMessageState.REFUSED, note=exc.message)
        return _Outcome(pivot, PauseMessageState.REFUSED, exc.conversation_id)
    except (RunHelpError, MachineSetupError):
        _set_state(help_request_id, PauseMessageState.DROPPED)
        return _Outcome(pivot, PauseMessageState.DROPPED, None)
    _set_state(help_request_id, PauseMessageState.SENT)
    return _Outcome(pivot, PauseMessageState.SENT, thread.pk)


def _writable_member(project_id: int, user: CustomUser | None) -> CustomUser | None:
    """The pauser while still an active writable member of the project."""
    if user is None or not user.is_active:
        return None
    access = get_project_access(project_id, user)
    if access is None or not access.can_write:
        return None
    return user


def _thread_id(help_request_id: int) -> int | None:
    return (
        SetupConversation.objects.filter(
            help_request_id=help_request_id, archived_at__isnull=True
        )
        .values_list("pk", flat=True)
        .first()
    )


def _set_state(
    help_request_id: int, state: PauseMessageState, *, note: str = ""
) -> None:
    # Conditional: only a pending state moves, so a second delivery is a no-op.
    CaseHelpRequest.objects.filter(
        pk=help_request_id, pause_message_state=PauseMessageState.PENDING
    ).update(
        pause_message_state=state,
        pause_message_note=note[:PAUSE_MESSAGE_NOTE_MAX_LENGTH],
    )


def _broadcast(help_request_id: int, outcome: _Outcome) -> None:
    """Tell open pages: the thread's help panel and the case (to navigate).

    Never raises: a page only misses a live update.
    """
    pivot = outcome.pivot
    project_id = pivot.test_run.project_id
    discussion_url = ""
    if outcome.conversation_id is not None:
        broadcast_help_thread(project_id, outcome.conversation_id, help_request_id)
        if outcome.state in (PauseMessageState.SENT, PauseMessageState.REFUSED):
            discussion_url = reverse(
                "machine_setup:thread",
                kwargs={
                    "project_id": project_id,
                    "conversation_id": outcome.conversation_id,
                },
            )
    try:
        broadcast_case_status(pivot, discussion_url=discussion_url)
    except Exception:
        logger.exception(
            "Paused help request %s: case status broadcast failed", help_request_id
        )
