"""Use case: a member resets the main chat, so the next message starts afresh."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.events import broadcast_reset
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.services.live_turn import mark_stop_requested, resolve_live_turn
from projects.models import Project


@transaction.atomic
def reset_conversation(*, project: Project, user: CustomUser) -> None:
    """Archive the main chat and stop its live turn, if any.

    Auto-fix threads are left alone, and so is a live auto-fix turn. Sending
    stays refused until the stopped turn has actually ended (or gone stale),
    so two agents never drive the machine at once.
    """
    Project.objects.select_for_update().only("id").get(pk=project.pk)
    now = timezone.now()
    chat_id = (
        SetupConversation.objects.filter(
            project=project,
            kind=SetupConversationKind.CHAT,
            archived_at__isnull=True,
        )
        .values_list("id", flat=True)
        .first()
    )
    turn = resolve_live_turn(project_id=project.pk, now=now)
    if turn is not None and chat_id is not None and turn.conversation_id == chat_id:
        mark_stop_requested(turn, user, reset=True)
    if chat_id is not None:
        SetupConversation.objects.filter(pk=chat_id).update(
            archived_at=now, archived_by=user
        )
    project_id = project.pk
    transaction.on_commit(lambda: broadcast_reset(project_id))
