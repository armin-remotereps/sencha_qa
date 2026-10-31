"""The project's knowledge base storage quota, checked under the project lock."""

from __future__ import annotations

from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import Coalesce

from knowledge_base.models import KnowledgeItem
from projects.models import Project


def lock_project_usage(project_id: int) -> int | None:
    """Lock the project row and return the bytes its items use.

    Must run inside a transaction. None when the project does not exist. The
    lock serialises quota checks of uploads and archive expansions.
    """
    locked = list(
        Project.objects.select_for_update()
        .filter(pk=project_id)
        .values_list("pk", flat=True)
    )
    if not locked:
        return None
    used: int = KnowledgeItem.objects.filter(project_id=project_id).aggregate(
        total=Coalesce(Sum("size_bytes"), 0)
    )["total"]
    return used


def quota_bytes() -> int:
    limit: int = settings.KNOWLEDGE_BASE_MAX_PROJECT_BYTES
    return limit


def fits_quota(used: int, size: int) -> bool:
    return used + size <= quota_bytes()
