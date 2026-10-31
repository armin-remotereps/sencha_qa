"""The weighted full-text vectors of an item's chunks."""

from __future__ import annotations

from django.contrib.postgres.search import SearchVector
from django.db.models import Value

from knowledge_base.models import KnowledgeChunk
from knowledge_base.search_config import KNOWLEDGE_SEARCH_CONFIG


def write_search_vectors(item_id: int, name: str, description: str) -> None:
    """(Re)write every chunk's vector of the item in one UPDATE.

    A: item name + description, B: chunk heading, C: chunk text.
    """
    KnowledgeChunk.objects.filter(item_id=item_id).update(
        search_vector=SearchVector(
            Value(f"{name} {description}"),
            weight="A",
            config=KNOWLEDGE_SEARCH_CONFIG,
        )
        + SearchVector("heading", weight="B", config=KNOWLEDGE_SEARCH_CONFIG)
        + SearchVector("text", weight="C", config=KNOWLEDGE_SEARCH_CONFIG)
    )
