"""List a project's knowledge base items (no chunk text is ever loaded).

Every function here costs one query, except ``get_knowledge_item_detail`` for
an archive member (one more to resolve its folder instructions).
``summaries_of`` and ``entries_of`` are shared with the other read services.
"""

from __future__ import annotations

from datetime import datetime

from django.db.models import (
    BooleanField,
    Count,
    ExpressionWrapper,
    F,
    Q,
    QuerySet,
    Sum,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.models import KnowledgeItem, KnowledgeKind
from knowledge_base.services.folder_instructions import instructions_for_items
from knowledge_base.services.item_retry_policy import can_retry
from knowledge_base.services.tag_filter import item_tag_q
from knowledge_base.types import (
    KnowledgeFolderRef,
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
    "folder",
    "tags",
    "is_folder_instructions",
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
    "parent__name",
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
        folder,
        tags,
        is_folder_instructions,
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
        folder=str(folder or ""),
        tags=_tags(tags),
        is_folder_instructions=bool(is_folder_instructions),
    )


def _int(value: object) -> int:
    return value if isinstance(value, int) else 0


def _tags(value: object) -> tuple[str, ...]:
    return tuple(str(tag) for tag in value) if isinstance(value, list) else ()


def summaries_of(queryset: QuerySet[KnowledgeItem]) -> list[KnowledgeItemSummary]:
    """Summaries of ``queryset`` (built on ``project_items``), in its order."""
    return [_summary(tuple(row)) for row in queryset.values_list(*_SUMMARY_FIELDS)]


def _display_name(first: object, last: object, email: object) -> str:
    full = f"{first or ''} {last or ''}".strip()
    return full or (str(email) if email else "")


def _entry(row: tuple[object, ...], now: datetime) -> KnowledgeListEntry:
    summary = _summary(row)
    offset = len(_SUMMARY_FIELDS)
    (
        error,
        warning,
        first,
        last,
        email,
        created_at,
        started_at,
        updated_at,
        archive_name,
    ) = row[offset : len(_ENTRY_FIELDS)]
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
        archive_name=str(archive_name or ""),
    )


def entries_of(
    queryset: QuerySet[KnowledgeItem], *, limit: int | None = None
) -> list[KnowledgeListEntry]:
    """List rows of ``queryset`` (built on ``project_items``), in its order."""
    now = timezone.now()
    rows = queryset.values_list(*_ENTRY_FIELDS)
    if limit is not None:
        rows = rows[:limit]
    return [_entry(tuple(row), now) for row in rows]


def _ordered(
    queryset: QuerySet[KnowledgeItem], parent_id: int | None, tag: str | None
) -> QuerySet[KnowledgeItem]:
    if parent_id is not None:
        # Archive members: by their path inside the archive.
        members = queryset.filter(parent_id=parent_id)
        if tag:
            members = members.filter(item_tag_q(tag))
        return members.order_by("folder", "name", "id")
    if tag:
        # Every level: newest upload first, an archive followed by its
        # matching files in path order.
        return queryset.filter(item_tag_q(tag)).order_by(
            Coalesce(F("parent_id"), F("id")).desc(),
            F("parent_id").asc(nulls_first=True),
            "folder",
            "name",
            "id",
        )
    return queryset.filter(parent__isnull=True).order_by("-created_at", "-id")


def list_knowledge_items(
    project_id: int, *, parent_id: int | None = None, tag: str | None = None
) -> list[KnowledgeItemSummary]:
    """Top-level items, or one archive's members (``parent_id``); all statuses.

    With ``tag`` (a manual tag or a folder path, subfolders included) and no
    ``parent_id`` every level is searched; with ``parent_id`` only that archive.
    """
    return summaries_of(_ordered(project_items(project_id), parent_id, tag))


def list_knowledge_entries(
    project_id: int,
    *,
    parent_id: int | None = None,
    limit: int | None = None,
    tag: str | None = None,
) -> list[KnowledgeListEntry]:
    """The Knowledge base tab's rows (summary plus page-only fields), in the
    order of ``list_knowledge_items``; at most ``limit`` when given (pass
    limit + 1 to detect more)."""
    return entries_of(_ordered(project_items(project_id), parent_id, tag), limit=limit)


def get_knowledge_item_detail(project_id: int, item_id: int) -> KnowledgeItemDetail:
    """The item page in one query; an archive member adds one query for its
    folder instructions (no excerpt). An archive's files come from
    ``browse_knowledge_archive``."""
    now = timezone.now()
    row = project_items(project_id).filter(id=item_id).values_list(*_DETAIL_FIELDS)
    own = row.first()
    if own is None:
        raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
    values = tuple(own)
    entry = _entry(values, now)
    original_name, char_count, chunk_count = values[len(_ENTRY_FIELDS) :]
    summary = entry.summary
    instructions = (
        instructions_for_items(
            project_id,
            [
                KnowledgeFolderRef(
                    item_id=summary.id,
                    parent_id=summary.parent_id,
                    folder=summary.folder,
                )
            ],
        ).get(summary.id, ())
        if summary.parent_id is not None
        else ()
    )
    return KnowledgeItemDetail(
        entry=entry,
        original_name=str(original_name),
        char_count=_int(char_count),
        chunk_count=_int(chunk_count),
        folder_instructions=instructions,
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
