"""Write an earlier artifacts profile back as a new revision (so it is undoable)."""

from __future__ import annotations

from accounts.models import CustomUser
from machine_readiness.exceptions import ArtifactProfileVersionNotFoundError
from machine_readiness.models import ArtifactProfileVersion, ProfileWriteSource
from machine_readiness.services.artifact_profile import write_profile
from machine_readiness.types import ProfileData


def restore_profile_version(
    *, project_id: int, version_id: int, author: CustomUser
) -> int:
    """Restore ``version_id``; returns the new revision.

    Raises ``ArtifactProfileVersionNotFoundError`` for another project's or a
    pruned version, and ``ArtifactProfileInvalidError`` when the snapshot no
    longer passes today's rules (e.g. a lower size cap). Knowledge base ids the
    version cited are accepted even if those items were deleted since.
    """
    version = (
        ArtifactProfileVersion.objects.filter(
            pk=version_id, profile__project_id=project_id
        )
        .only("revision", "snapshot")
        .first()
    )
    if version is None:
        raise ArtifactProfileVersionNotFoundError(
            "This version of the artifacts profile does not exist."
        )
    data = ProfileData.from_json(version.snapshot)
    return write_profile(
        project_id=project_id,
        data=data,
        source=ProfileWriteSource.RESTORE,
        author=author,
        change_summary=f"Restored revision {version.revision}",
        expected_revision=None,
        accept_knowledge_ids=data.knowledge_item_ids(),
    )
