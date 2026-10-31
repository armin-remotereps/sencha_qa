from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from knowledge_base.models import KnowledgeItem


@admin.register(KnowledgeItem)
class KnowledgeItemAdmin(admin.ModelAdmin[KnowledgeItem]):
    """Read-only: items are managed on the project's Knowledge base tab."""

    list_display = ("id", "project", "name", "kind", "status", "size_bytes")
    list_select_related = ("project",)
    list_filter = ("status", "kind")
    search_fields = ("name", "original_name")
    fields = (
        "project",
        "parent",
        "name",
        "original_name",
        "kind",
        "description",
        "status",
        "error",
        "warning",
        "size_bytes",
        "char_count",
        "page_count",
        "chunk_count",
        "extractor_version",
        "uploaded_by",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: KnowledgeItem | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: KnowledgeItem | None = None
    ) -> bool:
        return False
