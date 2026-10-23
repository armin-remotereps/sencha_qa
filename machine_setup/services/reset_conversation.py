"""Use case: a member resets the chat, so the next message starts afresh."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.events import broadcast_reset
from machine_setup.models import SetupConversation
from machine_setup.services.live_turn import mark_stop_requested, resolve_live_turn
from projects.models import Project


@transaction.atomic
def reset_conversation(*, project: Project, user: CustomUser) -> None:
    """Stop any live turn and archive the active conversation.

    Sending stays refused until the stopped turn has actually ended (or gone
    stale), so two agents never drive the machine at once.
    """
    Project.objects.select_for_update().only("id").get(pk=project.pk)
    now = timezone.now()
    turn = resolve_live_turn(project_id=project.pk, now=now)
    if turn is not None:
        mark_stop_requested(turn, user, reset=True)
    SetupConversation.objects.filter(project=project, archived_at__isnull=True).update(
        archived_at=now, archived_by=user
    )
    project_id = project.pk
    transaction.on_commit(lambda: broadcast_reset(project_id))
