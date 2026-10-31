"""The one text form of an artifacts profile (agent tool, planner, help leads).

A leaf module: it imports only ``machine_readiness`` models/types and the
knowledge base's non-raising lookup, so ``projects.services`` and
``machine_setup`` can read the profile without importing any service package.
"""

from __future__ import annotations

from collections.abc import Mapping


from accounts.models import CustomUser
from knowledge_base.services.find_knowledge_items import find_knowledge_items
from machine_readiness.models import (
    LATEST_VERSION,
    ArtifactItem,
    ArtifactKind,
    ArtifactOs,
    ArtifactProfile,
    ProfileWriteSource,
)
from machine_readiness.types import ProfileData, ProfileItemData, ProfileSnapshot

# A cited knowledge base item's name is cut to this in the text, so a rename
# can never push a profile that passed the write-time cap far over it.
KB_NAME_MAX_CHARS = 120

_OS_LABELS: tuple[tuple[ArtifactOs, str], ...] = (
    (ArtifactOs.WINDOWS, "Windows"),
    (ArtifactOs.MACOS, "macOS"),
    (ArtifactOs.LINUX, "Linux"),
)


def kb_label(item_id: int, kb_names: Mapping[int, str | None]) -> str:
    """'KB item #12 «Name»', or 'KB item #12 (deleted)' when absent or None."""
    name = kb_names.get(item_id)
    if name is None:
        return f"KB item #{item_id} (deleted)"
    if len(name) > KB_NAME_MAX_CHARS:
        name = name[: KB_NAME_MAX_CHARS - 1] + "…"
    return f"KB item #{item_id} «{name}»"


def render_profile_body(data: ProfileData, *, kb_names: Mapping[int, str | None]) -> str:
    """Base directories, notes and items; what the write-time cap measures."""
    lines = ["Base directories (one per test machine OS):"]
    for os_name, label in _OS_LABELS:
        lines.append(f"- {label}: {data.base_dir_for(os_name).strip() or '(not set)'}")
    notes = data.notes.strip()
    if notes:
        lines += ["", "Notes:", notes]
    if data.items:
        lines += ["", f"Artifacts ({len(data.items)}):"]
        for index, item in enumerate(data.items, start=1):
            lines += _item_lines(index, item, kb_names)
    else:
        lines += ["", "Artifacts: none recorded."]
    return "\n".join(lines)


def render_profile(
    snapshot: ProfileSnapshot, *, kb_names: Mapping[int, str | None]
) -> str:
    """The full profile text: a provenance line plus ``render_profile_body``.

    ``kb_names`` maps cited knowledge base ids to names; an id that is absent
    or maps to None renders as "KB item #N (deleted)". Pure.
    """
    header = f"Artifacts profile, revision {snapshot.revision}"
    if snapshot.written_at is not None:
        source = (
            ProfileWriteSource(snapshot.source).label
            if snapshot.source in ProfileWriteSource.values
            else "unknown"
        )
        header += (
            f" (last written by {source}, "
            f"{snapshot.written_at:%Y-%m-%d %H:%M} UTC)"
        )
    return f"{header}\n{render_profile_body(snapshot.data, kb_names=kb_names)}"


def load_profile_snapshot(project_id: int) -> ProfileSnapshot:
    """The project's profile; ``exists=False`` while intake is pending.

    Two queries (profile with its authors, then items); none for the KB.
    """
    profile = (
        ArtifactProfile.objects.filter(project_id=project_id)
        .select_related("written_by", "intake_skipped_by")
        .first()
    )
    if profile is None:
        return ProfileSnapshot(data=ProfileData(), exists=False)
    items = tuple(
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
    )
    return ProfileSnapshot(
        data=ProfileData(
            base_dir_windows=profile.base_dir_windows,
            base_dir_macos=profile.base_dir_macos,
            base_dir_linux=profile.base_dir_linux,
            notes=profile.notes,
            items=items,
        ),
        exists=True,
        revision=profile.revision,
        source=profile.source,
        written_by_id=profile.written_by_id,
        written_by_name=_user_name(profile.written_by),
        written_at=profile.written_at,
        change_summary=profile.change_summary,
        intake_skipped_at=profile.intake_skipped_at,
        intake_skipped_by_name=_user_name(profile.intake_skipped_by),
    )


def knowledge_names(project_id: int, data: ProfileData) -> dict[int, str | None]:
    """Names of the KB items ``data`` cites; deleted ones are absent.

    One query when any item cites a KB id, none otherwise.
    """
    ids = data.knowledge_item_ids()
    if not ids:
        return {}
    return {
        item_id: summary.name
        for item_id, summary in find_knowledge_items(project_id, ids).items()
    }


def artifacts_profile_text(project_id: int) -> str | None:
    """The profile text for prompts, or None without a profile or when empty.

    Two queries, plus one KB lookup when items cite KB ids.
    """
    snapshot = load_profile_snapshot(project_id)
    if not snapshot.exists or snapshot.data.is_empty:
        return None
    return render_profile(snapshot, kb_names=knowledge_names(project_id, snapshot.data))


def intake_pending(project_id: int) -> bool:
    """True while the project has no profile row (one ``exists()`` query)."""
    return not ArtifactProfile.objects.filter(project_id=project_id).exists()


def _item_lines(
    index: int, item: ProfileItemData, kb_names: Mapping[int, str | None]
) -> list[str]:
    kind = (
        ArtifactKind(item.kind).label
        if item.kind in ArtifactKind.values
        else item.kind or "Other"
    )
    lines = [f"{index}. [{item.key}] {item.name} ({kind})"]
    if item.path_pattern:
        lines.append(f"   Path: {item.path_pattern}")
    if item.expected_version:
        version = (
            "latest available"
            if item.expected_version.strip().lower() == LATEST_VERSION
            else item.expected_version
        )
        lines.append(f"   Expected version: {version}")
    if item.source:
        lines.append(f"   Source: {item.source}")
    if item.install_hint:
        lines.append(f"   Install: {_one_block(item.install_hint)}")
    if item.knowledge_item_id is not None:
        lines.append(f"   Knowledge base: {kb_label(item.knowledge_item_id, kb_names)}")
    return lines


def _one_block(text: str) -> str:
    """Multi-line hints stay inside their item: continuation lines indented."""
    return "\n     ".join(line.rstrip() for line in text.strip().splitlines())


def _user_name(user: CustomUser | None) -> str:
    if user is None:
        return ""
    return user.get_full_name().strip() or user.email
