"""Use case: a person sends a chat message, which starts one agent turn.

The person is a member on the web, or a person at the project's Test Runner
(a member, or anonymous; ``PersonStepActor``): a runner send stores no author
but an ``author_label`` naming it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.services.resolve_knowledge_refs import resolve_knowledge_refs
from machine_setup.document_text import UploadedDocument
from machine_setup.exceptions import (
    MachineSetupError,
    SetupDocumentNotAllowedError,
    SetupKnowledgeRefError,
    SetupTurnActiveError,
)
from machine_setup.models import SetupConversation, SetupConversationKind, SetupTurn
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.queue_turn import (
    broadcast_and_enqueue,
    create_user_turn,
    link_knowledge_refs,
)
from machine_setup.services.send_gates import (
    TRIAGE_RUN_GONE_MESSAGE,
    manual_thread_send_refusal,
    thread_send_refusal,
    triage_thread_send_refusal,
)
from machine_setup.services.thread_runner import (
    RUNNER_CHANGED_RETRY_MESSAGE,
    lock_runner,
    main_chat_runner_id,
    thread_runner_id,
)
from projects.models import PersonStepChannel, Project
from projects.person_step_actor import PersonStepActor
from runners.models import TestRunner

THREAD_CLOSED_MESSAGE = (
    "This conversation was reset. Send your message in the current chat."
)


def send_setup_message(
    *,
    project: Project,
    actor: PersonStepActor,
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
    ``SetupMachineNotReadyError`` (help, result and auto-fix threads only:
    the main chat, a manual case discussion and a case analysis may run with
    the machine offline), ``MachineSetupError`` for a case analysis whose run
    was deleted, ``MachineSetupError`` for a manual case discussion whose case
    is no longer claimed (its own started manual run does not refuse it), or ``MachineSetupError`` when the Test
    Runner is being updated or the thread was archived meanwhile, or ``SetupDocumentNotAllowedError`` when ``document``
    targets any thread but the main chat. A stale live turn is closed as
    ``worker_lost`` first, and stays closed even when the send is refused. A
    paused (Needs help) run or a started Manual run of the project does not
    block sending; a started agent-driven run on the thread's Test Runner,
    or any holder of that runner from another project, does. The turn
    records the thread's runner (``thread_runner``).
    """
    outcome = _send_locked(
        project=project,
        actor=actor,
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
    actor: PersonStepActor,
    text: str,
    conversation: SetupConversation | None,
    document: UploadedDocument | None,
    knowledge_item_ids: Sequence[int],
) -> SetupTurn | MachineSetupError:
    # The thread's Test Runner row first, then the project row (the lock
    # order of every runner holder; the project lock is the one start_test_run
    # and resume take), so a send and a run start cannot both pass their
    # "nothing else is live" checks.
    runner_id = (
        thread_runner_id(conversation)
        if conversation is not None
        else main_chat_runner_id(project.pk)
    )
    runner = lock_runner(runner_id)
    locked = Project.objects.select_for_update().only("id").get(pk=project.pk)
    kind = conversation.kind if conversation is not None else SetupConversationKind.CHAT
    refusal = _refusal(
        locked,
        runner,
        timezone.now(),
        kind=kind,
        manual_case_id=conversation.manual_case_id if conversation else None,
        triage_run_id=conversation.triage_run_id if conversation else None,
    )
    if refusal is not None:
        # Returned, not raised: a stale turn closed above must still commit.
        return refusal
    target = _target_conversation(locked, actor, conversation)
    if target is None:
        return MachineSetupError(THREAD_CLOSED_MESSAGE)
    if target.kind == SetupConversationKind.RUN_TRIAGE and target.triage_run_id is None:
        # The run was deleted since the caller loaded the thread.
        return MachineSetupError(TRIAGE_RUN_GONE_MESSAGE)
    if thread_runner_id(target) != runner_id:
        # Picked anew or the run was reassigned since the runner was locked.
        return MachineSetupError(RUNNER_CHANGED_RETRY_MESSAGE)
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
        conversation=target,
        requested_by=actor.user,
        text=text,
        runner_id=runner_id,
        document=document,
        author_label=_author_label(actor),
    )
    knowledge_refs = link_knowledge_refs(message, knowledge_items)
    transaction.on_commit(
        lambda: broadcast_and_enqueue(turn, message, documents, knowledge_refs)
    )
    return turn


def _author_label(actor: PersonStepActor) -> str:
    """The stored sender name of a Test Runner send; "" for the web (the
    author FK names the person)."""
    if actor.channel == PersonStepChannel.RUNNER:
        return actor.label()
    return ""


def _refusal(
    project: Project,
    runner: TestRunner | None,
    now: datetime,
    *,
    kind: str,
    manual_case_id: int | None,
    triage_run_id: int | None,
) -> MachineSetupError | None:
    # Phase 1 keeps one live turn per project (any runner).
    if resolve_live_turn(project_id=project.pk, now=now) is not None:
        return SetupTurnActiveError()
    if kind == SetupConversationKind.RUN_TRIAGE:
        # No runner gates: the analysis never uses the machine.
        return triage_thread_send_refusal(triage_run_id=triage_run_id)
    if kind == SetupConversationKind.MANUAL_CASE:
        # Under the project row lock, so a claim, release or hand-in (all
        # lock the project first) cannot change the case meanwhile.
        return manual_thread_send_refusal(project, runner, pivot_id=manual_case_id)
    main_chat = kind == SetupConversationKind.CHAT
    return thread_send_refusal(project, runner, kind=kind, main_chat=main_chat)


def _target_conversation(
    project: Project, actor: PersonStepActor, conversation: SetupConversation | None
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
        chat = SetupConversation.objects.create(project=project, created_by=actor.user)
    return chat
