"""Writing a category's groups (shared by AI grouping and a person's regroup).

Callers hold the turn gate (``require_running_turn``) in the open transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from django.db.models import Max
from django.utils import timezone

from run_triage.models import RunTriage, TriageCase, TriageGroup
from run_triage.taxonomy import TRIAGE_GROUP_TITLE_MAX_LENGTH, TRIAGE_NOTE_MAX_LENGTH
from run_triage.tool_text import cut, one_line


@dataclass(frozen=True)
class PlannedSubGroup:
    title: str
    row_ids: tuple[int, ...]


@dataclass(frozen=True)
class PlannedGroup:
    title: str
    row_ids: tuple[int, ...]
    sub_groups: tuple[PlannedSubGroup, ...]


def group_title(title: str) -> str:
    return cut(one_line(title), TRIAGE_GROUP_TITLE_MAX_LENGTH)


def next_position(triage_id: int, category: str) -> int:
    top = TriageGroup.objects.filter(triage_id=triage_id, category=category).aggregate(
        top=Max("position")
    )["top"]
    return 0 if top is None else int(top) + 1


def create_groups(
    *,
    triage_id: int,
    category: str,
    groups: Sequence[PlannedGroup],
    source: str,
    start_position: int,
) -> None:
    """Create ``groups`` and their sub-groups and move their rows into them
    (three or four queries whatever the size)."""
    tops = [
        TriageGroup(
            triage_id=triage_id,
            category=category,
            title=group_title(group.title),
            position=start_position + index,
            source=source,
        )
        for index, group in enumerate(groups)
    ]
    TriageGroup.objects.bulk_create(tops)
    subs: list[tuple[TriageGroup, PlannedSubGroup]] = []
    for top, group in zip(tops, groups):
        for index, sub in enumerate(group.sub_groups):
            subs.append(
                (
                    TriageGroup(
                        triage_id=triage_id,
                        category=category,
                        parent=top,
                        title=group_title(sub.title),
                        position=index,
                        source=source,
                    ),
                    sub,
                )
            )
    if subs:
        TriageGroup.objects.bulk_create([group for group, _ in subs])
    assigned: list[TriageCase] = []
    for top, group in zip(tops, groups):
        assigned.extend(TriageCase(id=row_id, group=top) for row_id in group.row_ids)
    for sub_group, sub in subs:
        assigned.extend(
            TriageCase(id=row_id, group=sub_group) for row_id in sub.row_ids
        )
    if assigned:
        TriageCase.objects.bulk_update(assigned, ["group"])


def write_note(
    triage_id: int, category: str, note: str | None, *, grouped: bool
) -> None:
    """Set (or, for an empty note, clear) a category note; mark the grouping time."""
    triage = (
        RunTriage.objects.select_for_update()
        .only("id", "category_notes", "last_grouped_at")
        .get(id=triage_id)
    )
    fields = ["updated_at"]
    if note is not None:
        notes: Mapping[str, object] = (
            triage.category_notes if isinstance(triage.category_notes, dict) else {}
        )
        updated = {str(key): value for key, value in notes.items()}
        text = cut(note.strip(), TRIAGE_NOTE_MAX_LENGTH)
        if text:
            updated[category] = text
        else:
            updated.pop(category, None)
        triage.category_notes = updated
        fields.append("category_notes")
    if grouped:
        triage.last_grouped_at = timezone.now()
        fields.append("last_grouped_at")
    triage.save(update_fields=fields)
