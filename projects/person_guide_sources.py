"""The project material behind Human in the loop "How to do it" guides.

``build_person_guide_sources`` gives ``agents`` lazy callables (it never
touches the ORM itself): the artifacts profile and the knowledge base index,
each read at most once per execution, the knowledge base excerpts related to
a step, and a check that cited items exist and are ready.
"""

from __future__ import annotations

from agents.types import PersonGuideSources, RelatedKnowledgeText
from knowledge_base.exceptions import KnowledgeBaseError
from knowledge_base.models import KnowledgeItemStatus
from knowledge_base.services.find_knowledge_items import find_knowledge_items
from knowledge_base.services.knowledge_index import knowledge_index_text
from knowledge_base.services.related_knowledge import related_knowledge
from knowledge_base.tool_text import render_excerpts
from machine_readiness.profile_text import artifacts_profile_with_items

# Excerpts per hand-off: whole chunks, at most this many and this long.
RELATED_EXCERPTS_LIMIT = 4
RELATED_EXCERPTS_MAX_CHARS = 5000


def build_person_guide_sources(project_id: int) -> PersonGuideSources:
    """Sources for one execution: build one per execution (the profile and
    the index are read at most once in its lifetime). A fresh plan reads its
    profile and index through these, so they are not read twice."""
    profile: list[tuple[str | None, tuple[int, ...]]] = []
    index: list[str | None] = []

    def load_profile() -> tuple[str | None, tuple[int, ...]]:
        # 2-3 queries, once per execution.
        if not profile:
            loaded = artifacts_profile_with_items(project_id)
            profile.append((None, ()) if loaded is None else loaded)
        return profile[0]

    def artifacts_profile() -> str | None:
        return load_profile()[0]

    def profile_item_ids() -> tuple[int, ...]:
        return load_profile()[1]

    def knowledge_index() -> str | None:
        # One query, once per execution.
        if not index:
            index.append(knowledge_index_text(project_id))
        return index[0]

    def related(text: str) -> RelatedKnowledgeText:
        try:
            excerpts = related_knowledge(
                project_id,
                text,
                limit=RELATED_EXCERPTS_LIMIT,
                max_chars=RELATED_EXCERPTS_MAX_CHARS,
            )
        except KnowledgeBaseError:
            return RelatedKnowledgeText(text="", item_names={})
        return RelatedKnowledgeText(
            text=render_excerpts(excerpts),
            item_names={excerpt.item_id: excerpt.item_name for excerpt in excerpts},
        )

    def resolve_items(ids: tuple[int, ...]) -> dict[int, str]:
        # One query; deleted, other-project and not-ready items are absent.
        return {
            item_id: summary.name
            for item_id, summary in find_knowledge_items(project_id, ids).items()
            if summary.status == KnowledgeItemStatus.READY
        }

    return PersonGuideSources(
        artifacts_profile=artifacts_profile,
        knowledge_index=knowledge_index,
        related=related,
        resolve_items=resolve_items,
        profile_item_ids=profile_item_ids,
    )
