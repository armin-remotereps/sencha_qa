"""Use case: a person opens the discussion of the Manual run case they are testing.

One non-archived ``manual_case`` thread per run case: an existing one is
reopened; otherwise it is created with the case brief as its first message.
Only a claimed IN_PROGRESS case of a started manual run can be discussed;
after the hand-in the thread stays readable and the result chat takes over.
The person's message is sent through the ordinary ``send_setup_message``
(manual gates), so the turn has the same limits and Stop as any setup turn.
"""

from __future__ import annotations

import logging

from django.db import transaction

from accounts.models import CustomUser
from agents.transcript import transcript_to_json
from agents.types import ChatMessage
from machine_setup.events import broadcast_message
from machine_setup.exceptions import MachineSetupError, ManualChatSendRefusedError
from machine_setup.models import (
    SetupConversation,
    SetupConversationKind,
    SetupMessage,
    SetupMessageKind,
)
from machine_setup.payloads import message_payload
from machine_setup.services.manual_case_context import (
    build_manual_brief,
    load_manual_case,
)
from machine_setup.services.send_message import send_setup_message
from projects.exceptions import ManualRunError
from projects.manual_case_lock import claim_matches, lock_manual_run_case
from projects.manual_run_policy import (
    MANUAL_DISCUSS_NOT_CLAIMED,
    MANUAL_PAUSE_STALE_CLAIM,
    is_claimed_manual_case,
)
from projects.models import Project, TestRunTestCase
from projects.person_step_actor import PersonStepActor

logger = logging.getLogger(__name__)


def find_manual_case(project: Project, pivot_id: int) -> TestRunTestCase | None:
    """The run case of this project, or None (the view's 404)."""
    return (
        TestRunTestCase.objects.filter(pk=pivot_id, test_run__project=project)
        .only("id", "test_run_id")
        .first()
    )


def open_manual_chat(
    *,
    project: Project,
    pivot_id: int,
    actor: PersonStepActor,
    text: str,
    claim_token: str | None = None,
) -> SetupConversation:
    """The case's discussion, with the person's message sent.

    ``actor``: a member on the web, or a person at the Test Runner (the
    message is then labelled, and anonymous when no member was named).
    Raises ``MachineSetupError`` (``MANUAL_DISCUSS_NOT_CLAIMED``) when the
    case is not a claimed IN_PROGRESS case of a started manual run of
    ``project`` (nothing is created), and ``ManualChatSendRefusedError`` when
    the thread is kept but the message was refused (a turn is live, the
    Test Runner is being updated, ...). An empty ``text`` sends nothing.
    ``claim_token`` (Test Runner sends) must still be the case's claim under
    the locks, else ``MANUAL_PAUSE_STALE_CLAIM``.
    """
    thread, brief_row = _open_thread(
        project=project, pivot_id=pivot_id, user=actor.user, claim_token=claim_token
    )
    if brief_row is not None:
        # After commit: a caller's outer transaction may still roll it back.
        row: SetupMessage = brief_row
        transaction.on_commit(lambda: _broadcast_brief(row))
    first = text.strip()
    if not first:
        return thread
    try:
        send_setup_message(
            project=project, actor=actor, text=first, conversation=thread
        )
    except MachineSetupError as exc:
        raise ManualChatSendRefusedError(
            exc.message, conversation_id=thread.pk
        ) from exc
    return thread


@transaction.atomic
def _open_thread(
    *,
    project: Project,
    pivot_id: int,
    user: CustomUser | None,
    claim_token: str | None,
) -> tuple[SetupConversation, SetupMessage | None]:
    """The case's thread, and its brief row when it was created now."""
    # Project, run, then case, as every manual service locks them, so a
    # release or hand-in cannot end the claim between the check and the thread.
    try:
        test_run, locked = lock_manual_run_case(
            project_id=project.pk, pivot_id=pivot_id
        )
    except ManualRunError as exc:
        raise MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED) from exc
    locked.test_run = test_run
    if not is_claimed_manual_case(locked.test_run, locked):
        raise MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED)
    if claim_token is not None and not claim_matches(locked, claim_token):
        raise MachineSetupError(MANUAL_PAUSE_STALE_CLAIM)
    existing = (
        SetupConversation.objects.filter(
            project=project, manual_case=locked, archived_at__isnull=True
        )
        .defer("llm_transcript")
        .first()
    )
    if existing is not None:
        return existing, None
    # The lock loads the case row only; the brief needs the test case and
    # the OS stored at claim.
    loaded = load_manual_case(locked.pk)
    if loaded is None:
        raise MachineSetupError(MANUAL_DISCUSS_NOT_CLAIMED)
    brief = build_manual_brief(loaded)
    thread = SetupConversation.objects.create(
        project=project,
        kind=SetupConversationKind.MANUAL_CASE,
        manual_case=locked,
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
        logger.exception("Manual chat %s: brief broadcast failed", row.conversation_id)
