"""Read and write a project's artifacts profile (versioned, conditional writes).

Every writer of the profile (the edit form, the setup agent, restore) goes
through ``write_profile``, so each replaced profile lands in the history.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from functools import partial

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.services.find_knowledge_items import find_knowledge_items
from knowledge_base.services.resolve_knowledge_refs import resolve_knowledge_refs
from machine_readiness.events import broadcast_readiness
from machine_readiness.exceptions import (
    ArtifactKnowledgeRefError,
    ArtifactProfileConflictError,
    ArtifactProfileInvalidError,
)
from machine_readiness.models import (
    PROFILE_CHANGE_SUMMARY_MAX_LENGTH,
    PROFILE_MAX_VERSIONS,
    ArtifactItem,
    ArtifactProfile,
    ArtifactProfileVersion,
    ProfileWriteSource,
)
from machine_readiness.profile_rules import validate_profile
from machine_readiness.profile_text import load_profile_snapshot
from machine_readiness.types import ProfileData, ProfileItemData, ProfileSnapshot


def get_profile(project_id: int) -> ProfileSnapshot:
    """The project's profile; ``exists=False`` while intake is pending. Two queries."""
    return load_profile_snapshot(project_id)


def write_profile(
    *,
    project_id: int,
    data: ProfileData,
    source: ProfileWriteSource,
    author: CustomUser | None,
    change_summary: str,
    expected_revision: int | None,
    accept_knowledge_ids: Collection[int] = (),
) -> int:
    """Replace the whole profile, keeping the old one in the history.

    Returns the new revision (the current one when ``data`` is unchanged).
    ``expected_revision`` makes the write conditional: it raises
    ``ArtifactProfileConflictError`` if the profile moved since then. Cited
    knowledge base ids must be items of the project, except ids the saved
    profile already cites (a deleted item never blocks a save) and
    ``accept_knowledge_ids`` (restore: ids the restored version cited).
    Raises ``ArtifactProfileInvalidError`` (``ArtifactKnowledgeRefError`` for
    refused KB ids).
    """
    validate_profile(data, change_summary=change_summary)
    with transaction.atomic():
        profile, _ = ArtifactProfile.objects.select_for_update().get_or_create(
            project_id=project_id
        )
        if expected_revision is not None and expected_revision != profile.revision:
            raise ArtifactProfileConflictError(
                f"The artifacts profile changed since revision {expected_revision}; "
                f"it is now at revision {profile.revision}. Read it again and "
                "apply your changes to the current version.",
                current_revision=profile.revision,
            )
        current = _current_data(profile)
        if profile.revision > 0 and current == data:
            return profile.revision
        _check_knowledge_refs(
            project_id,
            data,
            known=set(current.knowledge_item_ids()) | set(accept_knowledge_ids),
        )

        now = timezone.now()
        if profile.revision > 0:
            ArtifactProfileVersion.objects.create(
                profile=profile,
                revision=profile.revision,
                snapshot=current.to_json(),
                source=profile.source,
                written_by_id=profile.written_by_id,
                written_at=profile.written_at,
                change_summary=profile.change_summary,
                replaced_at=now,
            )
        profile.base_dir_windows = data.base_dir_windows
        profile.base_dir_macos = data.base_dir_macos
        profile.base_dir_linux = data.base_dir_linux
        profile.notes = data.notes
        profile.revision += 1
        profile.source = source
        profile.written_by = author
        profile.written_at = now
        profile.change_summary = change_summary[:PROFILE_CHANGE_SUMMARY_MAX_LENGTH]
        profile.save(
            update_fields=[
                "base_dir_windows",
                "base_dir_macos",
                "base_dir_linux",
                "notes",
                "revision",
                "source",
                "written_by",
                "written_at",
                "change_summary",
                "updated_at",
            ]
        )
        ArtifactItem.objects.filter(profile=profile).delete()
        ArtifactItem.objects.bulk_create(
            _item_row(profile, position, item)
            for position, item in enumerate(data.items, start=1)
        )
        _prune_history(profile)
        transaction.on_commit(
            partial(
                broadcast_readiness,
                project_id,
                {
                    "event": "profile",
                    "revision": profile.revision,
                    "intake_pending": False,
                },
            )
        )
        return profile.revision


def _current_data(profile: ArtifactProfile) -> ProfileData:
    """The saved profile as ``ProfileData`` (one query for the items)."""
    return ProfileData(
        base_dir_windows=profile.base_dir_windows,
        base_dir_macos=profile.base_dir_macos,
        base_dir_linux=profile.base_dir_linux,
        notes=profile.notes,
        items=tuple(
            ProfileItemData(
                key=item.key,
                name=item.name,
                kind=item.kind,
                path_pattern=item.path_pattern,
                expected_version=item.expected_version,
                source=item.source,
                install_hint=item.install_hint,
                knowledge_item_id=item.knowledge_item_id,
            )
            for item in ArtifactItem.objects.filter(profile=profile).order_by(
                "position", "id"
            )
        ),
    )


def _check_knowledge_refs(
    project_id: int, data: ProfileData, *, known: set[int]
) -> None:
    """Refuse cited KB ids that are not items of the project; one query at most."""
    new_ids = [item_id for item_id in data.knowledge_item_ids() if item_id not in known]
    if not new_ids:
        return
    try:
        resolve_knowledge_refs(project_id, new_ids)
    except KnowledgeItemNotFoundError:
        pass
    else:
        return
    # resolve_knowledge_refs names only the first missing id; find them all
    # (one more query, on this error path only) so every citing field is marked.
    found = find_knowledge_items(project_id, new_ids)
    refused = [item_id for item_id in new_ids if item_id not in found]
    raise ArtifactKnowledgeRefError(
        [
            f"Knowledge base item #{item_id} does not exist in this project "
            "(it may have been deleted)."
            for item_id in refused
        ],
        item_ids=refused,
    )


def _item_row(
    profile: ArtifactProfile, position: int, item: ProfileItemData
) -> ArtifactItem:
    return ArtifactItem(
        profile=profile,
        position=position,
        key=item.key,
        name=item.name,
        kind=item.kind,
        path_pattern=item.path_pattern,
        expected_version=item.expected_version,
        source=item.source,
        install_hint=item.install_hint,
        knowledge_item_id=item.knowledge_item_id,
    )


def _prune_history(profile: ArtifactProfile) -> None:
    keep = list(
        ArtifactProfileVersion.objects.filter(profile=profile)
        .order_by("-revision")
        .values_list("pk", flat=True)[:PROFILE_MAX_VERSIONS]
    )
    ArtifactProfileVersion.objects.filter(profile=profile).exclude(pk__in=keep).delete()
