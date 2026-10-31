"""Rename, describe and tag a knowledge item."""

from __future__ import annotations

from collections.abc import Sequence

from django.db import transaction

from auto_tester.file_names import sanitize_file_name
from knowledge_base.exceptions import KnowledgeItemNotFoundError
from knowledge_base.models import (
    KNOWLEDGE_DESCRIPTION_MAX,
    KNOWLEDGE_NAME_MAX_LENGTH,
    KnowledgeItem,
)
from knowledge_base.services.item_processing import ITEM_NOT_FOUND_MESSAGE
from knowledge_base.services.search_vectors import write_search_vectors
from knowledge_base.tags import merge_tags


def edit_knowledge_item(
    *,
    project_id: int,
    item_id: int,
    name: str,
    description: str,
    tags: Sequence[str],
) -> None:
    """Save the sanitised name, description and tags, then re-weight the vectors.

    An empty name keeps the current one. Tags are normalised, de-duplicated
    and capped; an archive's tags are not copied to its members.
    """
    with transaction.atomic():
        item = (
            KnowledgeItem.objects.select_for_update()
            .only("pk", "name", "description", "folder", "tags")
            .filter(pk=item_id, project_id=project_id)
            .first()
        )
        if item is None:
            raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
        item.name = sanitize_file_name(name, fallback=item.name)[
            :KNOWLEDGE_NAME_MAX_LENGTH
        ]
        item.description = description.strip()[:KNOWLEDGE_DESCRIPTION_MAX]
        item.tags = list(merge_tags(tags))
        item.save(update_fields=["name", "description", "tags", "updated_at"])
        write_search_vectors(
            item.pk,
            name=item.name,
            description=item.description,
            folder=item.folder,
            tags=item.tags,
        )
