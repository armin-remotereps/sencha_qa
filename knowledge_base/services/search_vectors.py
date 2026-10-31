"""The weighted full-text vectors of an item's chunks."""

from __future__ import annotations

from collections.abc import Sequence

from django.contrib.postgres.search import SearchVector
from django.db.models import Value

from knowledge_base.models import KnowledgeChunk
from knowledge_base.search_config import KNOWLEDGE_SEARCH_CONFIG
from knowledge_base.tags import search_label_text


def write_search_vectors(
    item_id: int,
    *,
    name: str,
    description: str,
    folder: str,
    tags: Sequence[str],
) -> None:
    """(Re)write every chunk's vector of the item in one UPDATE.

    A: item name + description, B: folder words + manual tags + chunk
    heading, C: chunk text. Folder words weigh like a heading so a generic
    folder does not rank every chunk like an item named after it.
    """
    KnowledgeChunk.objects.filter(item_id=item_id).update(
        search_vector=SearchVector(
            Value(f"{name} {description}"),
            weight="A",
            config=KNOWLEDGE_SEARCH_CONFIG,
        )
        + SearchVector(
            Value(search_label_text(folder, tags)),
            "heading",
            weight="B",
            config=KNOWLEDGE_SEARCH_CONFIG,
        )
        + SearchVector("text", weight="C", config=KNOWLEDGE_SEARCH_CONFIG)
    )
