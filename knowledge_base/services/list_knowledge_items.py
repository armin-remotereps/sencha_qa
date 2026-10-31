"""List a project's knowledge base items (no chunk text is ever loaded).

Every function here costs one query. ``summaries_of`` is shared with the
other read services that return ``KnowledgeItemSummary`` rows.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import BooleanField, Count, ExpressionWrapper, Q, QuerySet, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.models import KnowledgeItem, KnowledgeKind
from knowledge_base.services.item_retry_policy import can_retry
from knowledge_base.types import (
    KnowledgeItemDetail,
    KnowledgeItemSummary,
    KnowledgeListEntry,
)

ITEM_NOT_FOUND_MESSAGE = (
    "This knowledge base item does not exist (it may have been deleted)."
)

_SUMMARY_FIELDS = (
    "id",
    "name",
    "kind",
    "description",
    "status",
    "size_bytes",
    "page_count",
    "parent_id",
    "member_count",
)
_ENTRY_FIELDS = _SUMMARY_FIELDS + (
    "error",
    "warning",
    "uploaded_by__first_name",
    "uploaded_by__last_name",
    "uploaded_by__email",
    "created_at",
    "extract_started_at",
    "updated_at",
)
_DETAIL_FIELDS = _ENTRY_FIELDS + ("original_name", "char_count", "chunk_count")


def kind_label(kind: str) -> str:
    """The display label of a kind; the raw value for an unknown one."""
    try:
        return str(KnowledgeKind(kind).label)
    except ValueError:
        return kind


def project_items(project_id: int) -> QuerySet[KnowledgeItem]:
    """The project's items, annotated with ``member_count``."""
    return KnowledgeItem.objects.filter(project_id=project_id).annotate(
        member_count=Count("members")
    )


def _summary(row: tuple[object, ...]) -> KnowledgeItemSummary:
    (
        item_id,
        name,
        kind,
        description,
        status,
        size_bytes,
        page_count,
        parent_id,
        member_count,
    ) = row[: len(_SUMMARY_FIELDS)]
    assert isinstance(item_id, int)
    assert isinstance(parent_id, int) or parent_id is None
    return KnowledgeItemSummary(
        id=item_id,
        name=str(name),
        kind=str(kind),
        kind_label=kind_label(str(kind)),
        description=str(description),
        status=str(status),
        size_bytes=_int(size_bytes),
        page_count=_int(page_count),
        parent_id=parent_id,
        member_count=_int(member_count),
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def summaries_of(queryset: QuerySet[KnowledgeItem]) -> list[KnowledgeItemSummary]:
    """Summaries of ``queryset`` (built on ``project_items``), in its order."""
    return [_summary(tuple(row)) for row in queryset.values_list(*_SUMMARY_FIELDS)]


def _display_name(first: object, last: object, email: object) -> str:
    full = f"{first or ''} {last or ''}".strip()
    return full or (str(email) if email else "")


def _entry(row: tuple[object, ...], now: datetime) -> KnowledgeListEntry:
    summary = _summary(row)
    offset = len(_SUMMARY_FIELDS)
    error, warning, first, last, email, created_at, started_at, updated_at = row[
        offset : offset + 8
    ]
    assert isinstance(created_at, datetime)
    assert isinstance(started_at, datetime) or started_at is None
    assert isinstance(updated_at, datetime)
    return KnowledgeListEntry(
        summary=summary,
        error=str(error),
        warning=str(warning),
        uploaded_by=_display_name(first, last, email),
        created_at=created_at,
        can_retry=can_retry(summary.status, started_at, now, waiting_since=updated_at),
    )


def _ordered(
    queryset: QuerySet[KnowledgeItem], parent_id: int | None
) -> QuerySet[KnowledgeItem]:
    if parent_id is None:
        return queryset.filter(parent__isnull=True).order_by("-created_at", "-id")
    # Archive members: by their path inside the archive.
    return queryset.filter(parent_id=parent_id).order_by("original_name", "id")


def list_knowledge_items(
    project_id: int, *, parent_id: int | None = None
) -> list[KnowledgeItemSummary]:
    """Top-level items (``parent_id`` None) or one archive's members; all statuses."""
    return summaries_of(_ordered(project_items(project_id), parent_id))


def list_knowledge_entries(
    project_id: int, *, parent_id: int | None = None
) -> list[KnowledgeListEntry]:
    """The Knowledge base tab's rows (summary plus page-only fields)."""
    now = timezone.now()
    rows = _ordered(project_items(project_id), parent_id).values_list(*_ENTRY_FIELDS)
    return [_entry(tuple(row), now) for row in rows]


def get_knowledge_item_detail(project_id: int, item_id: int) -> KnowledgeItemDetail:
    """The item page: the item and its archive members, in one query."""
    now = timezone.now()
    rows = (
        project_items(project_id)
        .filter(Q(id=item_id) | Q(parent_id=item_id))
        .order_by("original_name", "id")
        .values_list(*_DETAIL_FIELDS)
    )
    own: tuple[object, ...] | None = None
    members: list[KnowledgeListEntry] = []
    for raw in rows:
        row = tuple(raw)
        if row[0] == item_id:
            own = row
        else:
            members.append(_entry(row, now))
    if own is None:
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    original_name, char_count, chunk_count = own[len(_ENTRY_FIELDS) :]
    return KnowledgeItemDetail(
        entry=_entry(own, now),
        original_name=str(original_name),
        char_count=_int(char_count),
        chunk_count=_int(chunk_count),
        members=tuple(members),
    )


def list_knowledge_choices(
    project_id: int, *, limit: int
) -> list[KnowledgeItemSummary]:
    """Composer picker: top-level items first, then archive members, newest first."""
    if limit <= 0:
        return []
    is_member = ExpressionWrapper(Q(parent__isnull=False), output_field=BooleanField())
    queryset = project_items(project_id).order_by(
        is_member.asc(), "-created_at", "-id"
    )[:limit]
    return summaries_of(queryset)


def knowledge_usage_bytes(project_id: int) -> int:
    """Bytes counted against the quota: every item, archives and members alike."""
    total = KnowledgeItem.objects.filter(project_id=project_id).aggregate(
        total=Coalesce(Sum("size_bytes"), 0)
    )["total"]
    return _int(total)
