"""Domain errors of the knowledge base; every ``message`` is safe to show."""

from __future__ import annotations


class KnowledgeBaseError(Exception):
    """Base class; ``message`` is safe to show to a member or an agent."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class KnowledgeUploadInvalidError(KnowledgeBaseError):
    """An upload was refused (type, size, count, disabled format)."""


class KnowledgeQuotaExceededError(KnowledgeBaseError):
    """The project's knowledge base would exceed its storage quota."""


class KnowledgeItemNotFoundError(KnowledgeBaseError):
    """No such item in this project (or it was deleted)."""


class KnowledgeFolderNotFoundError(KnowledgeBaseError):
    """No such folder in this archive."""


class KnowledgeItemNotReadyError(KnowledgeBaseError):
    """The item has no extracted text yet (waiting, reading or failed)."""


class KnowledgePageOutOfRangeError(KnowledgeBaseError):
    """The requested read page does not exist."""


class KnowledgeQueryInvalidError(KnowledgeBaseError):
    """A search query is empty or too long."""


class KnowledgeExtractionError(KnowledgeBaseError):
    """Extraction failed for a reason the member can act on."""


class UnsupportedContent(KnowledgeExtractionError):
    """The file holds nothing the extractor can read."""


class ContentMismatch(KnowledgeExtractionError):
    """The content does not match the file's extension."""


class ArchiveRejected(KnowledgeExtractionError):
    """An archive or container broke the archive guard's limits."""


class EncryptedDocument(KnowledgeExtractionError):
    """The document or archive is password protected."""


class TooLarge(KnowledgeExtractionError):
    """The extracted content exceeds a configured limit."""


class LegacyFormatUnreadable(KnowledgeExtractionError):
    """A Word 97-2003 or Excel 97-2003 file could not be read."""


class KnowledgeToolError(KnowledgeBaseError):
    """An agent's knowledge base tool call was refused; ``message`` goes back to it."""
