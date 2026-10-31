"""Skip intake: release the main chat's intake gate without writing a profile."""

from __future__ import annotations

from functools import partial

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from machine_readiness.events import broadcast_readiness
from machine_readiness.models import ArtifactProfile


def skip_intake(*, project_id: int, author: CustomUser) -> None:
    """Create the (empty) profile row, which marks intake done.

    Idempotent: when a row exists (written or skipped earlier) nothing
    changes. One query, two on a first skip (``get_or_create``). A first
    skip tells open Machine setup pages to hide the intake banner.
    """
    _, created = ArtifactProfile.objects.get_or_create(
        project_id=project_id,
        defaults={"intake_skipped_at": timezone.now(), "intake_skipped_by": author},
    )
    if created:
        transaction.on_commit(
            partial(broadcast_readiness, project_id, {"event": "intake_skipped"})
        )
