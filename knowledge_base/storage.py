"""The private file storage of knowledge base blobs (never under MEDIA_ROOT)."""

from __future__ import annotations

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class KnowledgeBaseStorage(FileSystemStorage):
    """Has no public URL: files are served only by the members-only view."""

    def url(self, name: str | None) -> str:
        raise NotImplementedError("Knowledge base files have no public URL.")


def knowledge_storage() -> KnowledgeBaseStorage:
    return KnowledgeBaseStorage(location=settings.KNOWLEDGE_BASE_ROOT)
