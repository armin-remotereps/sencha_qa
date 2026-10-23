"""Use case: a member asks the live setup turn to stop."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_setup.exceptions import SetupNoActiveTurnError
from machine_setup.services.live_turn import mark_stop_requested, resolve_live_turn
from projects.models import Project


def request_stop(*, project: Project, user: CustomUser) -> None:
    """Flag the live turn to stop at its next iteration or tool boundary.

    A stale turn is closed as ``worker_lost`` instead. Raises
    ``SetupNoActiveTurnError`` when no turn is live (after that close).
    """
    if not _request_stop_locked(project=project, user=user):
        raise SetupNoActiveTurnError()


@transaction.atomic
def _request_stop_locked(*, project: Project, user: CustomUser) -> bool:
    Project.objects.select_for_update().only("id").get(pk=project.pk)
    turn = resolve_live_turn(project_id=project.pk, now=timezone.now())
    if turn is None:
        return False
    return mark_stop_requested(turn, user)
