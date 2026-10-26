"""Use case: a person opens the result chat of a finished run case.

One non-archived thread per case, reused across executions: an existing one
is reopened; otherwise a ``case_result`` thread is created with the brief as
its first message. The person's message is then sent through the ordinary
``send_setup_message``, so the turn gets the same gates, limits and Stop as
any setup turn.
"""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import CustomUser
from agents.transcript import transcript_to_json
from agents.types import ChatMessage
from machine_setup.events import broadcast_message
from machine_setup.exceptions import MachineSetupError, ResultChatSendRefusedError
from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
)
from machine_setup.payloads import message_payload
from machine_setup.services.case_result_context import (
    build_result_brief,
    load_result_case,
)
from machine_setup.services.send_message import send_setup_message
from projects.case_result_change_services import is_result_changeable
from projects.models import Project, TestRunTestCase
from projects.run_reopen_services import lock_project_and_run

logger = logging.getLogger(__name__)

DEFAULT_FIRST_MESSAGE = (
    "Review this result and tell me whether it looks right, with the evidence."
)
NOT_CHANGEABLE_MESSAGE = (
    "Only a Passed, Failed or Blocked case can be discussed in a result chat."
)


def find_result_case(project: Project, pivot_id: int) -> TestRunTestCase | None:
    """The run case of this project, or None (the view's 404)."""
    return (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project=project)
        .only("id", "test_run_id")
        .first()
    )


def open_result_chat(
    *, project: Project, pivot: TestRunTestCase, user: CustomUser, text: str
) -> SetupConversation:
    """The case's result chat, with the person's message sent.

    ``pivot`` comes from ``find_result_case``. Raises ``MachineSetupError``
    when the case has no result to discuss (nothing is created), and
    ``ResultChatSendRefusedError`` when the thread is kept but the message
    was refused (a turn is live, a run is started, the machine is not
    ready, ...). An empty ``text`` asks for a review on a new thread and
    sends nothing on a reopened one.
    """
    thread, brief_row = _open_thread(project=project, pivot=pivot, user=user)
    if brief_row is not None:
        _broadcast_brief(brief_row)
    first = text.strip()
    if not first:
        if brief_row is None:
            return thread
        first = DEFAULT_FIRST_MESSAGE
    try:
        send_setup_message(project=project, user=user, text=first, conversation=thread)
    except MachineSetupError as exc:
        raise ResultChatSendRefusedError(
            exc.message, conversation_id=thread.pk
        ) from exc
    return thread


@transaction.atomic
def _open_thread(
    *, project: Project, pivot: TestRunTestCase, user: CustomUser
) -> tuple[SetupConversation, SetupMessage | None]:
    """The case's thread, and its brief row when it was created now."""
    # Same order as the result services (project, then run), so an Apply or
    # a re-run cannot change the case between the check and the thread.
    lock_project_and_run(pivot.test_run_id)
    loaded = load_result_case(pivot.pk)
    if loaded is None or not is_result_changeable(loaded):
        raise MachineSetupError(NOT_CHANGEABLE_MESSAGE)
    existing = (
        SetupConversation.objects.filter(
            project=project, result_case=loaded, archived_at__isnull=True
        )
        .defer("llm_transcript")
        .first()
    )
    if existing is not None:
        return existing, None
    brief = build_result_brief(loaded)
    thread = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.CASE_RESULT,
        result_case=loaded,
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
            message_payload(row, screenshots=[], attachments=[]),
        )
    except Exception:
        logger.exception("Result chat %s: brief broadcast failed", row.conversation_id)
