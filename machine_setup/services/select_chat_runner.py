"""Use case: a member picks the Test Runner of the project's main setup chat.

The main chat drives ``SetupConversation.runner`` (``thread_runner``). Only
a runner the member may assign to the project (their own, not archived,
while they are a writable member: ``runner_access.can_assign``) can be
picked; "none" clears it. Refused while a turn of the main chat is live, so
a running turn never finds its runner swapped (its tools would refuse).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.exceptions import MachineSetupError, SetupTurnActiveError
from machine_setup.models import SetupConversation, SetupConversationKind
from machine_setup.services.live_turn import resolve_live_turn
from machine_setup.services.thread_runner import lock_runner
from projects.models import Project
from runners.services.runner_access import can_assign
from runners.types import RunnerId

RUNNER_NOT_ALLOWED_MESSAGE = "You can pick only one of your own Test Runners."


@transaction.atomic
def select_chat_runner(
    *, project: Project, actor: CustomUser, runner_id: RunnerId | None
) -> SetupConversation:
    """Set (or clear, with None) the main chat's Test Runner.

    Locks the runner row, then the project row (the order of every turn
    acquisition). Creates the main chat when missing. Raises
    ``MachineSetupError`` when the runner may not be picked by ``actor``,
    ``SetupTurnActiveError`` while a turn of the main chat is live.
    """
    runner = lock_runner(runner_id)
    locked = (
        Project.objects.select_for_update().only("id", "archived").get(pk=project.pk)
    )
    if runner_id is not None and (
        runner is None or not can_assign(actor, runner, locked)
    ):
        raise MachineSetupError(RUNNER_NOT_ALLOWED_MESSAGE)
    chat = (
        SetupConversation.objects.filter(
            project=locked, kind=SetupConversationKind.CHAT, archived_at__isnull=True
        )
        .defer("llm_transcript")
        .first()
    )
    if chat is None:
        return SetupConversation.objects.create(
            project=locked, created_by=actor, runner_id=runner_id
        )
    live = resolve_live_turn(project_id=locked.pk, now=timezone.now())
    if live is not None and live.conversation_id == chat.pk:
        raise SetupTurnActiveError()
    if chat.runner_id != runner_id:
        chat.runner_id = runner_id
        chat.save(update_fields=["runner", "updated_at"])
    return chat
