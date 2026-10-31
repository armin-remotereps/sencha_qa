"""Server-side retrieval of whole chunks related to a text (the Ask AI advisor).

The text is split into word tokens, each becomes a plain ``SearchQuery`` and
they are OR-ed: no tsquery syntax is ever built from user text. One query; an
empty knowledge base simply matches nothing.
"""

from __future__ import annotations

import re

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F, OrderBy

from knowledge_base.models import KnowledgeChunk
from knowledge_base.search_config import KNOWLEDGE_SEARCH_CONFIG
from knowledge_base.services.search_knowledge import ready_chunks, top_chunk_ids
from knowledge_base.types import KnowledgeExcerpt

MAX_TOKENS = 30
CHUNKS_PER_ITEM = 2
# A last excerpt shorter than this after cutting is dropped rather than kept.
_MIN_CUT_CHARS = 200
_ELLIPSIS = "…"
_TOKEN = re.compile(r"[\w.-]{3,}")
_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "are", "but", "not", "you", "your", "all", "any",
        "can", "has", "have", "had", "was", "were", "this", "that", "these",
        "those", "with", "from", "into", "onto", "then", "than", "there", "their",
        "they", "them", "what", "when", "where", "which", "who", "whom", "why",
        "how", "will", "would", "should", "could", "does", "did", "doing", "done",
        "been", "being", "its", "our", "out", "off", "over", "under", "about",
        "after", "before", "again", "also", "just", "only", "some", "such",
        "very", "too", "here", "each", "both", "more", "most", "other", "own",
        "same", "may", "might", "must", "shall", "step", "steps", "please",
        "use", "using", "make", "sure", "should", "need", "needs", "want",
    }
)  # fmt: skip


def related_tokens(text: str) -> list[str]:
    """Up to ``MAX_TOKENS`` distinct lower-case tokens, stop words removed."""
    tokens: dict[str, None] = {}
    for raw in _TOKEN.findall(text):
        token = raw.strip(".-").lower()
        if len(token) < 3 or token in _STOP_WORDS:
            continue
        tokens[token] = None
        if len(tokens) >= MAX_TOKENS:
            break
    return list(tokens)


def related_knowledge(
    project_id: int, text: str, *, limit: int, max_chars: int
) -> list[KnowledgeExcerpt]:
    """The best ``limit`` whole chunks (at most 2 per item), ``max_chars`` in total."""
    tokens = related_tokens(text)
    if limit <= 0 or max_chars <= 0 or not tokens:
        return []
    search_query = SearchQuery(
        tokens[0], search_type="plain", config=KNOWLEDGE_SEARCH_CONFIG
    )
    for token in tokens[1:]:
        search_query = search_query | SearchQuery(
            token, search_type="plain", config=KNOWLEDGE_SEARCH_CONFIG
        )
    rank = SearchRank(F("search_vector"), search_query, cover_density=True)
    best = top_chunk_ids(
        ready_chunks(project_id).filter(search_vector=search_query).annotate(rank=rank),
        order=[OrderBy(F("rank"), descending=True), OrderBy(F("ordinal"))],
        per_item=CHUNKS_PER_ITEM,
        limit=limit,
    )
    rows = (
        KnowledgeChunk.objects.filter(id__in=best)
        .annotate(rank=rank)
        .order_by("-rank", "item_id", "ordinal")
        .values_list("item_id", "item__name", "location", "page", "text")
    )
    excerpts: list[KnowledgeExcerpt] = []
    budget = max_chars
    for item_id, item_name, location, page, chunk_text in rows:
        if budget <= 0:
            break
        if len(chunk_text) > budget:
            if budget < _MIN_CUT_CHARS and excerpts:
                break
            chunk_text = (
                chunk_text[: max(budget - len(_ELLIPSIS), 0)].rstrip() + _ELLIPSIS
            )
        budget -= len(chunk_text)
        excerpts.append(
            KnowledgeExcerpt(
                item_id=item_id,
                item_name=item_name,
                location=location,
                page=page,
                text=chunk_text,
            )
        )
    return excerpts
