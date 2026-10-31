from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone

from projects.models import Project

KNOWLEDGE_NAME_MAX_LENGTH = 255
KNOWLEDGE_DESCRIPTION_MAX = 1000
KNOWLEDGE_ERROR_MAX_LENGTH = 500
KNOWLEDGE_LOCATION_MAX_LENGTH = 120
KNOWLEDGE_HEADING_MAX_LENGTH = 255
# A member's directory inside its archive ("docs/install/windows").
KNOWLEDGE_FOLDER_MAX_LENGTH = 1024
KNOWLEDGE_TAG_MAX_LENGTH = 64
KNOWLEDGE_MAX_TAGS_PER_ITEM = 20
# Bumped when extraction output changes in a way worth re-extracting for.
KNOWLEDGE_EXTRACTOR_VERSION = 1


class KnowledgeKind(models.TextChoices):
    TEXT = "text", "Text"
    MARKDOWN = "markdown", "Markdown"
    CSV = "csv", "CSV / TSV"
    XML = "xml", "XML"
    JSON = "json", "JSON"
    YAML = "yaml", "YAML"
    CONFIG = "config", "Config"
    SCRIPT = "script", "Script"
    SOURCE = "source", "Source code"
    HTML = "html", "HTML"
    PDF = "pdf", "PDF"
    DOCX = "docx", "Word"
    DOC = "doc", "Word 97–2003"
    XLSX = "xlsx", "Excel"
    XLS = "xls", "Excel 97–2003"
    ARCHIVE = "archive", "Archive"


class KnowledgeItemStatus(models.TextChoices):
    PENDING = "pending", "Waiting"
    EXTRACTING = "extracting", "Reading"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class KnowledgeBlob(models.Model):
    """A file's bytes, stored once in the private KB storage by content hash.

    No project FK: a blob is reachable only through the items that use it,
    so duplicated projects share files.
    """

    sha256 = models.CharField(max_length=64, unique=True)
    size_bytes = models.BigIntegerField()
    # Name inside the KB storage ("ab/abcdef..."), never under MEDIA_ROOT.
    storage_name = models.CharField(max_length=200)
    # Set when stored and refreshed whenever an upload stores the same bytes
    # again: delete and the sweep leave blobs stored within the last hour,
    # which an upload in progress may be about to reference.
    stored_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.sha256


class KnowledgeItem(models.Model):
    """One uploaded file (or archive member) of a project's knowledge base."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="knowledge_items"
    )
    # Set on archive members: the archive item they were read from.
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="members",
    )
    blob = models.ForeignKey(
        KnowledgeBlob, on_delete=models.PROTECT, related_name="items"
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Sanitised display name (auto_tester.file_names.sanitize_file_name).
    name = models.CharField(max_length=KNOWLEDGE_NAME_MAX_LENGTH)
    # The upload's name, or the member path inside its archive (a label only).
    original_name = models.CharField(max_length=KNOWLEDGE_NAME_MAX_LENGTH)
    # An archive member's directory ("/" separators, no leading or trailing
    # slash; a nested archive's file name is one segment), "" at the root and
    # for top-level uploads. It is the item's folder tag: shown in its own
    # case, matched case-insensitively with subfolders, never edited.
    folder = models.CharField(
        max_length=KNOWLEDGE_FOLDER_MAX_LENGTH, blank=True, default=""
    )
    # Manual tags only (given at upload, inherited from the archive, or
    # edited), lower-case, at most KNOWLEDGE_MAX_TAGS_PER_ITEM
    # (knowledge_base.tags).
    tags = ArrayField(
        models.CharField(max_length=KNOWLEDGE_TAG_MAX_LENGTH),
        blank=True,
        default=list,
    )
    # Set at expansion on the one README / INSTRUCTIONS file of a folder
    # (knowledge_base.folder_instructions); it applies to that folder and
    # every subfolder of the same archive, resolved at read time.
    is_folder_instructions = models.BooleanField(default=False)
    kind = models.CharField(max_length=20, choices=KnowledgeKind.choices)
    description = models.TextField(
        max_length=KNOWLEDGE_DESCRIPTION_MAX, blank=True, default=""
    )
    status = models.CharField(
        max_length=20,
        choices=KnowledgeItemStatus.choices,
        default=KnowledgeItemStatus.PENDING,
    )
    # Safe text only (domain messages), never a traceback.
    error = models.CharField(
        max_length=KNOWLEDGE_ERROR_MAX_LENGTH, blank=True, default=""
    )
    warning = models.CharField(
        max_length=KNOWLEDGE_ERROR_MAX_LENGTH, blank=True, default=""
    )
    # A copy of the blob's size (the quota sums items, archives and members).
    size_bytes = models.BigIntegerField(default=0)
    char_count = models.IntegerField(default=0)
    page_count = models.IntegerField(default=0)
    chunk_count = models.IntegerField(default=0)
    extractor_version = models.PositiveSmallIntegerField(default=0)
    extract_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["project", "parent", "status"], name="kb_item_proj_parent"
            ),
            models.Index(
                fields=["project", "-created_at"], name="kb_item_proj_created"
            ),
            GinIndex(fields=["tags"], name="kb_item_tags"),
            models.Index(
                fields=["parent", "folder"],
                condition=models.Q(is_folder_instructions=True),
                name="kb_item_folder_instr",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class KnowledgeChunk(models.Model):
    """About 1500 characters of an item's text, with where it came from."""

    item = models.ForeignKey(
        KnowledgeItem, on_delete=models.CASCADE, related_name="chunks"
    )
    # A copy of the item's project, so search filters without a join.
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="+", db_index=False
    )
    ordinal = models.IntegerField()
    # 1-based read page (KNOWLEDGE_PAGE_CHARS per page; a chunk never splits).
    page = models.IntegerField()
    location = models.CharField(max_length=KNOWLEDGE_LOCATION_MAX_LENGTH)
    heading = models.CharField(
        max_length=KNOWLEDGE_HEADING_MAX_LENGTH, blank=True, default=""
    )
    text = models.TextField()
    # Weights: A item name + description, B heading, C text
    # (knowledge_base.search_config.KNOWLEDGE_SEARCH_CONFIG).
    search_vector = SearchVectorField(null=True)

    class Meta:
        indexes = [
            GinIndex(fields=["search_vector"], name="kb_chunk_search"),
            models.Index(fields=["item", "page", "ordinal"], name="kb_chunk_item_page"),
            models.Index(fields=["project"], name="kb_chunk_project"),
        ]

    def __str__(self) -> str:
        return f"{self.item_id}:{self.ordinal}"
