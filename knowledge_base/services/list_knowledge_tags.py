"""The tag filter's choices: manual tags and archive folders, with counts.

Two queries. The ORM cannot group by a set-returning function, so manual
tags are counted with parameterised SQL over ``unnest(tags)``.
"""

from __future__ import annotations

from django.db import connection
from django.db.models import Count

from knowledge_base.models import KnowledgeItem
from knowledge_base.types import KnowledgeTagCount, KnowledgeTagFacet

TAG_FACET_LIMIT = 300


def list_knowledge_tags(
    project_id: int, *, limit: int = TAG_FACET_LIMIT
) -> KnowledgeTagFacet:
    """Manual tag and folder counts of the project, each capped and by name."""
    if limit <= 0:
        return KnowledgeTagFacet(tags=(), folders=())
    folders = (
        KnowledgeItem.objects.filter(project_id=project_id)
        .exclude(folder="")
        .values("folder")
        .annotate(count=Count("id"))
        .order_by("folder")
        .values_list("folder", "count")[:limit]
    )
    return KnowledgeTagFacet(
        tags=_manual_tag_counts(project_id, limit),
        folders=tuple(
            KnowledgeTagCount(tag=folder, count=count) for folder, count in folders
        ),
    )


def _manual_tag_counts(project_id: int, limit: int) -> tuple[KnowledgeTagCount, ...]:
    table = connection.ops.quote_name(KnowledgeItem._meta.db_table)
    sql = (
        "SELECT t.tag, COUNT(*) "
        f"FROM {table} i CROSS JOIN LATERAL unnest(i.tags) AS t(tag) "
        "WHERE i.project_id = %s "
        "GROUP BY t.tag ORDER BY t.tag LIMIT %s"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [project_id, limit])
        rows = cursor.fetchall()
    return tuple(
        KnowledgeTagCount(tag=str(tag), count=int(count)) for tag, count in rows
    )
