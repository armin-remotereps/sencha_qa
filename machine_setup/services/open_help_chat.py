"""Use case: a person opens the help chat of a case that needs help.

One thread per help request: an existing thread (the Night auto-fix's, or a
help chat opened earlier) is reused; otherwise a ``case_help`` thread is
created with the person brief as its first message. The first message is then
sent through the ordinary ``send_setup_message``, so the turn gets the same
gates, limits and Stop as any setup turn.
"""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import CustomUser
from agents.transcript import transcript_to_json
from agents.types import ChatMessage
from machine_setup.events import broadcast_message
from machine_setup.exceptions import HelpChatSendRefusedError, MachineSetupError
from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
)
from machine_setup.payloads import message_payload
from machine_setup.services.case_help_context import (
    build_help_brief,
    load_help_request,
)
from machine_setup.services.send_message import send_setup_message
from projects.case_help_services import lock_help_request_for_chat
from projects.models import Project, TestRunTestCase

logger = logging.getLogger(__name__)

DEFAULT_FIRST_MESSAGE = (
    "Look at the test machine read-only, tell me what you see and what you suggest."
)
HELP_CHAT_CLOSED_MESSAGE = "This help chat was closed and cannot be reopened."


def open_help_chat(
    *, project: Project, pivot: TestRunTestCase, user: CustomUser, text: str
) -> SetupConversation:
    """The help request's thread, with the person's first message sent.

    Raises ``RunHelpError`` when the run is not paused or the case has no open
    request (nothing is created), ``MachineSetupError`` when the request's
    thread was archived, and ``HelpChatSendRefusedError`` when the thread is
    kept but the message was refused (a turn is live, the machine is not
    ready, ...). An empty ``text`` asks for a read-only look; reopening an
    existing help chat with an empty ``text`` sends nothing.
    """
    thread, brief_row = _open_thread(project=project, pivot_id=pivot.pk, user=user)
    created = brief_row is not None
    if brief_row is not None:
        _broadcast_brief(brief_row)
    first = text.strip()
    if not first:
        if not created and thread.kind == SetupConversationKind.CASE_HELP:
            return thread
        first = DEFAULT_FIRST_MESSAGE
    try:
        send_setup_message(project=project, user=user, text=first, conversation=thread)
    except MachineSetupError as exc:
        raise HelpChatSendRefusedError(exc.message, conversation_id=thread.pk) from exc
    return thread


def find_help_case(project: Project, pivot_id: int) -> TestRunTestCase | None:
    """The run case of this project, or None (the view's 404)."""
    return (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project=project)
        .only("id", "test_run_id")
        .first()
    )


@transaction.atomic
def _open_thread(
    *, project: Project, pivot_id: int, user: CustomUser
) -> tuple[SetupConversation, SetupMessage | None]:
    """The request's thread, and its brief row when it was created now."""
    # Locks project -> run -> request; releases a queued auto-fix to the person.
    request = lock_help_request_for_chat(project=project, pivot_id=pivot_id)
    existing = (
        SetupConversation.objects.filter(help_request=request)
        .defer("llm_transcript")
        .first()
    )
    if existing is not None:
        if existing.archived_at is not None:
            raise MachineSetupError(HELP_CHAT_CLOSED_MESSAGE)
        return existing, None

    # The lock helper loads the case row only; the brief needs case and run.
    brief_request = load_help_request(request.pk) or request
    brief = build_help_brief(brief_request, audience="person")
    thread = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.CASE_HELP,
        help_request=request,
        created_by=user,
        llm_transcript=transcript_to_json((ChatMessage(role="user", content=brief),)),
    )
    # The brief as the server's display row: no author, no turn.
    brief_row = SetupMessage.objects.create(
        conversation=thread,
        turn=None,
        kind=SetupMessageKind.USER,
        author=None,
        text=brief,
    )
    return thread, brief_row


def _broadcast_brief(row: SetupMessage) -> None:
    """Announce the brief row, so an open main chat marks this thread as another one.

    Never raises: the page only misses a live update.
    """
    try:
        broadcast_message(
            row.conversation.project_id,
            message_payload(row, screenshots=[], attachments=[], knowledge_refs=[]),
        )
    except Exception:
        logger.exception("Help chat %s: brief broadcast failed", row.conversation_id)
