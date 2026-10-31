"""The tag filter's choices: manual tags and archive folders, with counts."""

from __future__ import annotations

from knowledge_base.types import KnowledgeTagFacet

TAG_FACET_LIMIT = 300


def list_knowledge_tags(
    project_id: int, *, limit: int = TAG_FACET_LIMIT
) -> KnowledgeTagFacet:
    """Manual tag and folder counts of the project, each capped and by name."""
    raise NotImplementedError
