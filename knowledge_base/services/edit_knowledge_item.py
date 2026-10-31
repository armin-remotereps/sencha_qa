"""Rename and describe a knowledge item."""

from __future__ import annotations

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


def edit_knowledge_item(
    *, project_id: int, item_id: int, name: str, description: str
) -> None:
    """Save the sanitised name and the description, then re-weight the vectors.

    An empty name keeps the current one.
    """
    with transaction.atomic():
        item = (
            KnowledgeItem.objects.select_for_update()
            .only("pk", "name", "description")
            .filter(pk=item_id, project_id=project_id)
            .first()
        )
        if item is None:
            raise KnowledgeItemNotFoundError(ITEM_NOT_FOUND_MESSAGE)
        item.name = sanitize_file_name(name, fallback=item.name)[
            :KNOWLEDGE_NAME_MAX_LENGTH
        ]
        item.description = description.strip()[:KNOWLEDGE_DESCRIPTION_MAX]
        item.save(update_fields=["name", "description", "updated_at"])
        write_search_vectors(item.pk, item.name, item.description)
