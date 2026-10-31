"""Create a setup turn with its user message, then broadcast and enqueue it.

Shared by a person's send (``send_message``) and the server-started auto-fix
(``case_auto_fix``). The turn is enqueued by task name so this module does not
import ``machine_setup.tasks``, which imports the worker runtime (and through
it this package): a direct import would be a cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from auto_tester.celery import app as celery_app
from knowledge_base.types import KnowledgeItemSummary
from machine_setup.document_text import UploadedDocument
from machine_setup.events import broadcast_message, broadcast_turn
from machine_setup.models import (
    SetupConversation,
    SetupDocument,
    SetupMessage,
    SetupMessageKind,
    SetupTurn,
    SetupTurnEndReason,
    SetupTurnStatus,
)
from machine_setup.payloads import (
    KnowledgeRefPayload,
    knowledge_ref_payload,
    message_payload,
    turn_payload,
)

logger = logging.getLogger(__name__)

# The name `machine_setup.tasks.run_setup_turn` is registered under; routed to
# the `execution` queue by CELERY_TASK_ROUTES.
RUN_SETUP_TURN_TASK = "machine_setup.tasks.run_setup_turn"

ENQUEUE_FAILED_MESSAGE = "The setup agent could not be started. Try again."


def create_turn(
    *,
    conversation: SetupConversation,
    requested_by: CustomUser | None,
    text: str,
    auto_fix: bool = False,
) -> tuple[SetupTurn, SetupMessage]:
    """A queued turn plus its USER row; call under the project row lock."""
    turn = SetupTurn.objects.create(
        conversation=conversation,
        project_id=conversation.project_id,
        requested_by=requested_by,
        auto_fix=auto_fix,
    )
    message = SetupMessage.objects.create(
        conversation=conversation,
        turn=turn,
        kind=SetupMessageKind.USER,
        author=requested_by,
        text=text,
    )
    return turn, message


def create_user_turn(
    *,
    conversation: SetupConversation,
    requested_by: CustomUser,
    text: str,
    document: UploadedDocument | None = None,
) -> tuple[SetupTurn, SetupMessage, list[SetupDocument]]:
    """``create_turn`` for a person's send, storing an attached document with
    the USER row. Returns the stored documents so the broadcast needs no query.
    Call under the project row lock."""
    turn, message = create_turn(
        conversation=conversation, requested_by=requested_by, text=text
    )
    if document is None:
        return turn, message, []
    return (
        turn,
        message,
        [_store_document(conversation, message, requested_by, document)],
    )


def link_knowledge_refs(
    message: SetupMessage, items: Sequence[KnowledgeItemSummary]
) -> list[KnowledgeRefPayload]:
    """Link validated knowledge base items to a new USER row; one INSERT.

    Returns their chip payloads so the broadcast needs no query. Call in the
    transaction that created ``message``.
    """
    if not items:
        return []
    through = SetupMessage.knowledge_items.through
    through.objects.bulk_create(
        [
            through(setupmessage_id=message.pk, knowledgeitem_id=item.id)
            for item in items
        ]
    )
    project_id = message.conversation.project_id
    return [
        knowledge_ref_payload(
            project_id=project_id, item_id=item.id, name=item.name, status=item.status
        )
        for item in items
    ]


def _store_document(
    conversation: SetupConversation,
    message: SetupMessage,
    requested_by: CustomUser,
    document: UploadedDocument,
) -> SetupDocument:
    return SetupDocument.objects.create(
        conversation=conversation,
        message=message,
        uploaded_by=requested_by,
        file_name=document.file_name,
        text=document.text,
        size_bytes=document.size_bytes,
        char_count=len(document.text),
    )


def broadcast_and_enqueue(
    turn: SetupTurn,
    message: SetupMessage,
    documents: Sequence[SetupDocument] = (),
    knowledge_refs: Sequence[KnowledgeRefPayload] = (),
) -> bool:
    """Run on commit. Returns False when the task could not be queued.

    ``documents`` are the ones attached to ``message`` (already loaded), and
    ``knowledge_refs`` the chips of the items it references.

    The turn is then closed as ``failed/internal_error`` with a notice row.
    A failed broadcast is logged and never stops the enqueue: the page only
    misses a live update, while an unqueued turn would sit queued until stale.
    """
    # Broadcast the queued turn before enqueueing, so a fast worker's
    # "running" broadcast cannot be overtaken by a stale "queued" one.
    _broadcast(turn, message, documents, knowledge_refs)
    try:
        result = celery_app.send_task(RUN_SETUP_TURN_TASK, args=[turn.pk])
    except Exception:
        logger.exception("Could not queue setup turn %d", turn.pk)
        _fail_unqueued_turn(turn)
        return False
    try:
        SetupTurn.objects.filter(pk=turn.pk).update(celery_task_id=result.id or "")
    except Exception:
        # Queued all the same; the id only serves revocation and diagnostics.
        logger.exception("Setup turn %d: storing its task id failed", turn.pk)
    return True


def _broadcast(
    turn: SetupTurn,
    message: SetupMessage | None,
    documents: Sequence[SetupDocument] = (),
    knowledge_refs: Sequence[KnowledgeRefPayload] = (),
) -> None:
    """Send the turn (and its message) to the chat page; never raises."""
    try:
        if message is not None:
            broadcast_message(
                turn.project_id,
                message_payload(
                    message,
                    screenshots=[],
                    attachments=list(documents),
                    knowledge_refs=list(knowledge_refs),
                ),
            )
        broadcast_turn(turn.project_id, turn_payload(turn))
    except Exception:
        logger.exception("Setup turn %d: broadcast failed", turn.pk)


def _fail_unqueued_turn(turn: SetupTurn) -> None:
    now = timezone.now()
    with transaction.atomic():
        failed = SetupTurn.objects.filter(
            pk=turn.pk, status=SetupTurnStatus.QUEUED
        ).update(
            status=SetupTurnStatus.FAILED,
            ended_reason=SetupTurnEndReason.INTERNAL_ERROR,
            error_message=ENQUEUE_FAILED_MESSAGE,
            finished_at=now,
        )
        if not failed:
            return
        notice = SetupMessage.objects.create(
            conversation_id=turn.conversation_id,
            turn=turn,
            kind=SetupMessageKind.NOTICE,
            text=ENQUEUE_FAILED_MESSAGE,
        )
    turn.status = SetupTurnStatus.FAILED
    turn.ended_reason = SetupTurnEndReason.INTERNAL_ERROR
    turn.error_message = ENQUEUE_FAILED_MESSAGE
    turn.finished_at = now
    _broadcast(turn, notice)
