"""What the Machine readiness tab shows of the artifacts profile (read only)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from knowledge_base.services.find_knowledge_items import find_knowledge_items
from machine_readiness.models import (
    PROFILE_MAX_VERSIONS,
    ArtifactKind,
    ArtifactProfileVersion,
    ProfileWriteSource,
)
from machine_readiness.profile_text import kb_label, knowledge_names
from machine_readiness.services.artifact_profile import get_profile
from machine_readiness.types import ProfileData, ProfileItemData, ProfileSnapshot


@dataclass(frozen=True)
class ProfileItemRow:
    item: ProfileItemData
    kind_label: str
    # "KB item #12 «Name»" / "KB item #12 (deleted)"; "" without a citation.
    kb_label: str
    # False when the cited item is deleted (no link then).
    kb_exists: bool


@dataclass(frozen=True)
class ProfileProvenance:
    revision: int
    source_label: str
    author_name: str
    written_at: datetime | None
    change_summary: str
    intake_skipped_at: datetime | None
    intake_skipped_by_name: str


@dataclass(frozen=True)
class ProfileVersionRow:
    id: int
    revision: int
    source_label: str
    author_name: str
    written_at: datetime | None
    change_summary: str
    replaced_at: datetime


@dataclass(frozen=True)
class ProfileOverview:
    profile: ProfileSnapshot
    items: tuple[ProfileItemRow, ...]
    provenance: ProfileProvenance | None
    versions: tuple[ProfileVersionRow, ...]

    @property
    def intake_pending(self) -> bool:
        return self.profile.intake_pending


def get_profile_overview(
    project_id: int, *, snapshot: ProfileSnapshot | None = None
) -> ProfileOverview:
    """Profile, item rows with KB labels, provenance and history.

    Four queries at most: profile, items (both skipped when the caller
    passes the ``snapshot`` it loaded), cited KB names, versions.
    """
    if snapshot is None:
        snapshot = get_profile(project_id)
    if not snapshot.exists:
        return ProfileOverview(profile=snapshot, items=(), provenance=None, versions=())
    return ProfileOverview(
        profile=snapshot,
        items=profile_item_rows(project_id, snapshot.data),
        provenance=ProfileProvenance(
            revision=snapshot.revision,
            source_label=_source_label(snapshot.source),
            author_name=snapshot.written_by_name,
            written_at=snapshot.written_at,
            change_summary=snapshot.change_summary,
            intake_skipped_at=snapshot.intake_skipped_at,
            intake_skipped_by_name=snapshot.intake_skipped_by_name,
        ),
        versions=_version_rows(project_id),
    )


def profile_item_rows(project_id: int, data: ProfileData) -> tuple[ProfileItemRow, ...]:
    """``data``'s items with kind and KB labels; one query when any cites KB."""
    names = knowledge_names(project_id, data)
    return tuple(
        ProfileItemRow(
            item=item,
            kind_label=(
                ArtifactKind(item.kind).label
                if item.kind in ArtifactKind.values
                else item.kind
            ),
            kb_label=(
                kb_label(item.knowledge_item_id, names)
                if item.knowledge_item_id is not None
                else ""
            ),
            kb_exists=item.knowledge_item_id in names,
        )
        for item in data.items
    )


def kb_labels_for(project_id: int, ids: Collection[int]) -> dict[int, str]:
    """``kb_label`` of each id ("KB item #N (deleted)" when absent); one query."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    names: dict[int, str | None] = {
        item_id: summary.name
        for item_id, summary in find_knowledge_items(project_id, wanted).items()
    }
    return {item_id: kb_label(item_id, names) for item_id in wanted}


def _version_rows(project_id: int) -> tuple[ProfileVersionRow, ...]:
    versions = (
        ArtifactProfileVersion.objects.filter(profile__project_id=project_id)
        .select_related("written_by")
        .only(
            "revision",
            "source",
            "written_at",
            "change_summary",
            "replaced_at",
            "written_by__first_name",
            "written_by__last_name",
            "written_by__email",
        )
        .order_by("-revision")[:PROFILE_MAX_VERSIONS]
    )
    return tuple(
        ProfileVersionRow(
            id=version.pk,
            revision=version.revision,
            source_label=_source_label(version.source),
            author_name=(
                (version.written_by.get_full_name().strip() or version.written_by.email)
                if version.written_by is not None
                else ""
            ),
            written_at=version.written_at,
            change_summary=version.change_summary,
            replaced_at=version.replaced_at,
        )
        for version in versions
    )


def _source_label(source: str) -> str:
    if source in ProfileWriteSource.values:
        return str(ProfileWriteSource(source).label)
    return ""
