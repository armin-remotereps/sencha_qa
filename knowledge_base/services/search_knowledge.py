"""Search a project's ready knowledge base chunks (full text, or exact text).

FTS mode ranks with ``SearchRank`` over the GIN-indexed vector and computes
``«»`` headlines only for the returned hits; with no FTS hit it falls back to
exact mode once. Each mode is one query: the per-item cap and the limit are
applied in a subquery, the outer query fetches the few hits it returns.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from django.contrib.postgres.search import SearchHeadline, SearchQuery, SearchRank
from django.db.models import F, OrderBy, QuerySet, Subquery, Window
from django.db.models.functions import RowNumber

from knowledge_base.exceptions import KnowledgeQueryInvalidError
from knowledge_base.models import KnowledgeChunk, KnowledgeItemStatus
from knowledge_base.search_config import KNOWLEDGE_SEARCH_CONFIG
from knowledge_base.services.tag_filter import item_tag_q
from knowledge_base.types import KnowledgeHit

SEARCH_QUERY_MAX_LENGTH = 300
SEARCH_LIMIT_MIN = 1
SEARCH_LIMIT_MAX = 20
HITS_PER_ITEM = 3
HIGHLIGHT_START = "«"
HIGHLIGHT_STOP = "»"
# Characters of context on each side of an exact match.
EXACT_SNIPPET_CONTEXT = 120
_HEADLINE_MAX_WORDS = 35
_HEADLINE_MIN_WORDS = 15
_HEADLINE_MAX_FRAGMENTS = 2
_ELLIPSIS = "…"
_HIT_ITEM_FIELDS = ("item_id", "item__name", "item__folder", "item__parent_id")


def ready_chunks(project_id: int) -> QuerySet[KnowledgeChunk]:
    """Chunks of the project's ready items (the chunk's own project column)."""
    return KnowledgeChunk.objects.filter(
        project_id=project_id, item__status=KnowledgeItemStatus.READY
    )


def top_chunk_ids(
    chunks: QuerySet[KnowledgeChunk],
    *,
    order: Sequence[OrderBy],
    per_item: int,
    limit: int,
) -> Subquery:
    """A subquery of the ids of the best ``limit`` chunks, at most ``per_item`` per item."""
    return Subquery(
        chunks.annotate(
            item_row=Window(
                expression=RowNumber(),
                partition_by=[F("item_id")],
                order_by=list(order),
            )
        )
        .filter(item_row__lte=per_item)
        .order_by(*order)
        .values("id")[:limit]
    )


def clean_query(query: str) -> str:
    """The stripped query; raises ``KnowledgeQueryInvalidError`` when empty or too long."""
    cleaned = query.strip()
    if not cleaned:
        raise KnowledgeQueryInvalidError("The search query is empty.")
    if len(cleaned) > SEARCH_QUERY_MAX_LENGTH:
        raise KnowledgeQueryInvalidError(
            f"The search query is longer than {SEARCH_QUERY_MAX_LENGTH} characters."
        )
    return cleaned


def clamp_limit(limit: int) -> int:
    return max(SEARCH_LIMIT_MIN, min(SEARCH_LIMIT_MAX, limit))


def search_knowledge(
    project_id: int,
    query: str,
    *,
    limit: int,
    item_ids: Sequence[int] | None = None,
    exact: bool = False,
    tag: str | None = None,
) -> list[KnowledgeHit]:
    """Best hits for ``query``, at most 3 per item; ready items only.

    ``tag`` (a manual tag, or a folder path with its subfolders) narrows the
    search to the items it matches; None or "" does not filter.
    """
    cleaned = clean_query(query)
    limit = clamp_limit(limit)
    chunks = ready_chunks(project_id)
    if item_ids is not None:
        wanted = list(dict.fromkeys(item_ids))
        if not wanted:
            return []
        chunks = chunks.filter(item_id__in=wanted)
    if tag:
        chunks = chunks.filter(item_tag_q(tag, prefix="item__"))
    if not exact:
        hits = _full_text_hits(chunks, cleaned, limit)
        if hits:
            return hits
    return _exact_hits(chunks, cleaned, limit)


def _full_text_hits(
    chunks: QuerySet[KnowledgeChunk], query: str, limit: int
) -> list[KnowledgeHit]:
    search_query = SearchQuery(
        query, search_type="websearch", config=KNOWLEDGE_SEARCH_CONFIG
    )
    rank = SearchRank(F("search_vector"), search_query)
    order = [OrderBy(F("rank"), descending=True), OrderBy(F("ordinal"))]
    best = top_chunk_ids(
        chunks.filter(search_vector=search_query).annotate(rank=rank),
        order=order,
        per_item=HITS_PER_ITEM,
        limit=limit,
    )
    rows = (
        KnowledgeChunk.objects.filter(id__in=best)
        .annotate(
            rank=rank,
            headline=SearchHeadline(
                "text",
                search_query,
                config=KNOWLEDGE_SEARCH_CONFIG,
                start_sel=HIGHLIGHT_START,
                stop_sel=HIGHLIGHT_STOP,
                max_words=_HEADLINE_MAX_WORDS,
                min_words=_HEADLINE_MIN_WORDS,
                max_fragments=_HEADLINE_MAX_FRAGMENTS,
                fragment_delimiter=f" {_ELLIPSIS} ",
            ),
        )
        .order_by("-rank", "item_id", "ordinal")
        .values_list(*_HIT_ITEM_FIELDS, "location", "page", "headline")
    )
    return [
        KnowledgeHit(
            item_id=item_id,
            item_name=item_name,
            location=location,
            page=page,
            snippet=_one_line(str(headline)),
            item_folder=item_folder,
            item_parent_id=item_parent_id,
        )
        for item_id, item_name, item_folder, item_parent_id, location, page, headline in rows
    ]


def _exact_hits(
    chunks: QuerySet[KnowledgeChunk], query: str, limit: int
) -> list[KnowledgeHit]:
    # Newest items first, then document order.
    order = [OrderBy(F("item_id"), descending=True), OrderBy(F("ordinal"))]
    best = top_chunk_ids(
        chunks.filter(text__icontains=query),
        order=order,
        per_item=HITS_PER_ITEM,
        limit=limit,
    )
    rows = (
        KnowledgeChunk.objects.filter(id__in=best)
        .order_by("-item_id", "ordinal")
        .values_list(*_HIT_ITEM_FIELDS, "location", "page", "text")
    )
    return [
        KnowledgeHit(
            item_id=item_id,
            item_name=item_name,
            location=location,
            page=page,
            snippet=exact_snippet(text, query),
            item_folder=item_folder,
            item_parent_id=item_parent_id,
        )
        for item_id, item_name, item_folder, item_parent_id, location, page, text in rows
    ]


def exact_snippet(text: str, query: str) -> str:
    """A window around the first case-insensitive match, the match in ``«»``."""
    match = re.search(re.escape(query), text, flags=re.IGNORECASE)
    if match is None:
        # Postgres and Python case folding can disagree on rare characters.
        head = _one_line(text[: 2 * EXACT_SNIPPET_CONTEXT])
        return head + (_ELLIPSIS if len(text) > 2 * EXACT_SNIPPET_CONTEXT else "")
    start = max(0, match.start() - EXACT_SNIPPET_CONTEXT)
    end = min(len(text), match.end() + EXACT_SNIPPET_CONTEXT)
    before = _one_line(text[start : match.start()])
    after = _one_line(text[match.end() : end])
    return (
        (_ELLIPSIS if start > 0 else "")
        + before
        + (" " if before and text[match.start() - 1].isspace() else "")
        + HIGHLIGHT_START
        + _one_line(match.group(0))
        + HIGHLIGHT_STOP
        + (" " if after and text[match.end()].isspace() else "")
        + after
        + (_ELLIPSIS if end < len(text) else "")
    )


def _one_line(text: str) -> str:
    return " ".join(text.split())
