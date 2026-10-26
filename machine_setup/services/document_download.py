"""Use case: a member downloads a document attached to the project's main chat."""

from __future__ import annotations

from dataclasses import dataclass

from machine_setup.models import SetupDocument
from projects.models import Project


@dataclass(frozen=True)
class DocumentDownload:
    file_name: str
    text: str


def find_document(*, project: Project, document_id: int) -> DocumentDownload | None:
    """The document when it belongs to this project; one query."""
    row = (
        SetupDocument.objects.filter(pk=document_id, conversation__project=project)
        .values("file_name", "text")
        .first()
    )
    if row is None:
        return None
    return DocumentDownload(file_name=row["file_name"], text=row["text"])
