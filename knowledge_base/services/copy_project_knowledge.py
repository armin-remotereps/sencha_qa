"""Copy a project's ready knowledge items into a duplicated project."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from django.db import connection, transaction
from django.db.models import Q

from knowledge_base.models import KnowledgeChunk, KnowledgeItem, KnowledgeItemStatus
from knowledge_base.services.project_quota import fits_quota, lock_project_usage

logger = logging.getLogger(__name__)

# Items whose chunks are copied by one INSERT … SELECT.
CHUNK_COPY_ITEM_BATCH = 200

_COPIED_FIELDS = (
    "name",
    "original_name",
    "kind",
    "description",
    "warning",
    "size_bytes",
    "char_count",
    "page_count",
    "chunk_count",
    "extractor_version",
    "blob_id",
    "uploaded_by_id",
)


def copy_project_knowledge(*, source_project_id: int, target_project_id: int) -> int:
    """Copy ready items (and ready members of copied archives); return the count.

    Blobs are shared, chunks and their vectors are copied set-based. Does
    nothing when the target already has items (a repeated task) or the copy
    would exceed the target's quota.
    """
    with transaction.atomic():
        used = lock_project_usage(target_project_id)
        if used is None:
            return 0
        if KnowledgeItem.objects.filter(project_id=target_project_id).exists():
            return 0
        rows = list(
            KnowledgeItem.objects.filter(
                project_id=source_project_id, status=KnowledgeItemStatus.READY
            )
            .filter(
                Q(parent__isnull=True)
                | Q(
                    parent__status=KnowledgeItemStatus.READY,
                    parent__parent__isnull=True,
                )
            )
            .order_by("id")
            .values("id", "parent_id", *_COPIED_FIELDS)
        )
        if not rows:
            return 0
        total = sum(int(row["size_bytes"]) for row in rows)
        if not fits_quota(used, total):
            logger.warning(
                "Knowledge base of project %s not copied to project %s: "
                "%s bytes would exceed the quota",
                source_project_id,
                target_project_id,
                total,
            )
            return 0
        top_level = [row for row in rows if row["parent_id"] is None]
        members = [row for row in rows if row["parent_id"] is not None]
        id_map = _create_items(target_project_id, top_level, {})
        id_map.update(_create_items(target_project_id, members, id_map))
        _copy_chunks(target_project_id, id_map)
    return len(id_map)


def _create_items(
    target_project_id: int,
    rows: Sequence[dict[str, object]],
    parent_map: dict[int, int],
) -> dict[int, int]:
    """Insert copies of ``rows``; map each source id to its copy's id."""
    copies = [
        KnowledgeItem(
            project_id=target_project_id,
            parent_id=(
                parent_map[_as_int(row["parent_id"])]
                if row["parent_id"] is not None
                else None
            ),
            status=KnowledgeItemStatus.READY,
            **{field: row[field] for field in _COPIED_FIELDS},
        )
        for row in rows
    ]
    created = KnowledgeItem.objects.bulk_create(copies)
    return {
        _as_int(row["id"]): copy.pk for row, copy in zip(rows, created, strict=True)
    }


def _as_int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError("expected an integer id")
    return value


def _copy_chunks(target_project_id: int, id_map: dict[int, int]) -> None:
    table = connection.ops.quote_name(KnowledgeChunk._meta.db_table)
    sql = (
        f"INSERT INTO {table} "
        "(item_id, project_id, ordinal, page, location, heading, text, search_vector) "
        "SELECT m.new_id, %s, c.ordinal, c.page, c.location, c.heading, c.text, "
        "c.search_vector "
        f"FROM {table} c "
        "JOIN unnest(%s::bigint[], %s::bigint[]) AS m(old_id, new_id) "
        "ON c.item_id = m.old_id"
    )
    pairs = list(id_map.items())
    with connection.cursor() as cursor:
        for start in range(0, len(pairs), CHUNK_COPY_ITEM_BATCH):
            batch = pairs[start : start + CHUNK_COPY_ITEM_BATCH]
            cursor.execute(
                sql,
                [
                    target_project_id,
                    [old for old, _new in batch],
                    [new for _old, new in batch],
                ],
            )
