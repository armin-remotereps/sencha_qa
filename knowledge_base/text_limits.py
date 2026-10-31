"""Sizes of chunks and read pages (shared by chunking, reads and prompts)."""

from __future__ import annotations

# Characters per read_knowledge page; chunks are packed greedily, never split.
KNOWLEDGE_PAGE_CHARS = 12000
# Target and hard cap of one chunk's text.
CHUNK_TARGET_CHARS = 1500
CHUNK_MAX_CHARS = 3000
