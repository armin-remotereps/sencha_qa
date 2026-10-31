"""The Postgres text search configuration of knowledge base chunks."""

from __future__ import annotations

# Used both when writing ``KnowledgeChunk.search_vector`` and when querying it;
# the two must match or nothing is found.
KNOWLEDGE_SEARCH_CONFIG = "english"
