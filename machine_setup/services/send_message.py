"""Use case: a member sends a chat message, which starts one agent turn."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.services.resolve_knowledge_refs import resolve_knowledge_refs
from machine_setup.document_text import UploadedDocument
from machine_setup.exceptions import (
    MachineSetupError,
    SetupDocumentNotAllowedError,
    SetupKnowledgeRefError,
    SetupMachineNotReadyError,
    SetupRunActiveError,
    SetupTurnActiveError,
)
from machine_setup.models import SetupConversation, SetupConversationKind, SetupTurn
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.queue_turn import (
    broadcast_and_enqueue,
    create_user_turn,
    link_knowledge_refs,
)
from projects.models import Project, TestRun, TestRunStatus
from projects.runner_update_state import (
    RUNNER_UPDATE_RUNNING_MESSAGE,
    runner_update_in_progress,
)
from projects.services import MachineState, get_machine_state
from visual_engine.services.pool_status import get_visual_engine_status

THREAD_CLOSED_MESSAGE = (
    "This conversation was reset. Send your message in the current chat."
)


def send_setup_message(
    *,
    project: Project,
    user: CustomUser,
    text: str,
    conversation: SetupConversation | None = None,
    document: UploadedDocument | None = None,
    knowledge_item_ids: Sequence[int] = (),
) -> SetupTurn:
    """Record the user's message and queue a turn for it.

    ``knowledge_item_ids`` (any thread, already capped by the form) are linked
    to the USER row; ``SetupKnowledgeRefError`` when one is not an item of
    the project.

    ``conversation`` is a thread of this project to reply in (the main chat or
    an auto-fix thread); ``None`` means the main chat, created when missing.
    Raises ``SetupTurnActiveError``, ``SetupRunActiveError``,
    ``SetupMachineNotReadyError``, or ``MachineSetupError`` when the Test
    Runner is being updated or the thread was archived meanwhile, or ``SetupDocumentNotAllowedError`` when ``document``
    targets any thread but the main chat. A stale live turn is closed as
    ``worker_lost`` first, and stays closed even when the send is refused. A paused (Needs help) run
    does not block sending; a started one does.
    """
    outcome = _send_locked(
        project=project,
        user=user,
        text=text,
        conversation=conversation,
        document=document,
        knowledge_item_ids=knowledge_item_ids,
    )
    if isinstance(outcome, MachineSetupError):
        raise outcome
    return outcome


@transaction.atomic
def _send_locked(
    *,
    project: Project,
    user: CustomUser,
    text: str,
    conversation: SetupConversation | None,
    document: UploadedDocument | None,
    knowledge_item_ids: Sequence[int],
) -> SetupTurn | MachineSetupError:
    # Same lock as start_test_run and resume (project row first), so a send
    # and a run start cannot both pass their "nothing else is live" checks.
    locked = (
        Project.objects.select_for_update()
        .only("id", "agent_connected")
        .get(pk=project.pk)
    )
    refusal = _refusal(locked, timezone.now())
    if refusal is not None:
        # Returned, not raised: a stale turn closed above must still commit.
        return refusal
    target = _target_conversation(locked, user, conversation)
    if target is None:
        return MachineSetupError(THREAD_CLOSED_MESSAGE)
    if document is not None and target.kind != SetupConversationKind.CHAT:
        return SetupDocumentNotAllowedError()
    try:
        knowledge_items = (
            resolve_knowledge_refs(locked.pk, knowledge_item_ids)
            if knowledge_item_ids
            else []
        )
    except KnowledgeItemNotFoundError:
        return SetupKnowledgeRefError()
    turn, message, documents = create_user_turn(
        conversation=target, requested_by=user, text=text, document=document
    )
    knowledge_refs = link_knowledge_refs(message, knowledge_items)
    transaction.on_commit(
        lambda: broadcast_and_enqueue(turn, message, documents, knowledge_refs)
    )
    return turn


def _refusal(project: Project, now: datetime) -> MachineSetupError | None:
    if resolve_live_turn(project_id=project.pk, now=now) is not None:
        return SetupTurnActiveError()
    if TestRun.objects.filter(project=project, status=TestRunStatus.STARTED).exists():
        return SetupRunActiveError()
    if runner_update_in_progress(project.pk, now):
        return MachineSetupError(RUNNER_UPDATE_RUNNING_MESSAGE)
    visual_engine = get_visual_engine_status()
    if get_machine_state(project, visual_engine) != MachineState.READY:
        return SetupMachineNotReadyError()
    return None


def _target_conversation(
    project: Project, user: CustomUser, conversation: SetupConversation | None
) -> SetupConversation | None:
    """The thread to write to; ``None`` when the given thread was archived."""
    if conversation is not None:
        return (
            SetupConversation.objects.filter(
                pk=conversation.pk, project=project, archived_at__isnull=True
            )
            .defer("llm_transcript")
            .first()
        )
    chat = (
        SetupConversation.objects.filter(
            project=project,
            kind=SetupConversationKind.CHAT,
            archived_at__isnull=True,
        )
        .defer("llm_transcript")
        .first()
    )
    if chat is None:
        chat = SetupConversation.objects.create(project=project, created_by=user)
    return chat
