"""Skip intake: release the main chat's intake gate without writing a profile."""

from __future__ import annotations

from django.utils import timezone

from accounts.models import CustomUser
from machine_readiness.models import ArtifactProfile


def skip_intake(*, project_id: int, author: CustomUser) -> None:
    """Create the (empty) profile row, which marks intake done.

    Idempotent: when a row exists (written or skipped earlier) nothing
    changes. One query, two on a first skip (``get_or_create``).
    """
    ArtifactProfile.objects.get_or_create(
        project_id=project_id,
        defaults={"intake_skipped_at": timezone.now(), "intake_skipped_by": author},
    )
