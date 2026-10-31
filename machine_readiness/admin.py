from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from machine_readiness.models import (
    ArtifactInventory,
    ArtifactProfile,
    PreconditionAnalysis,
    Stage0Requirement,
)


class _ReadOnlyAdminMixin:
    """Rows are written on the Machine readiness tab and by the setup agent."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: object | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: object | None = None
    ) -> bool:
        return False


@admin.register(ArtifactProfile)
class ArtifactProfileAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[ArtifactProfile]):
    list_display = ("id", "project", "revision", "source", "written_at")
    list_select_related = ("project",)


@admin.register(ArtifactInventory)
class ArtifactInventoryAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[ArtifactInventory]):
    list_display = ("id", "project", "os_name", "host", "entry_count", "scanned_at")
    list_select_related = ("project",)

    def get_queryset(self, request: HttpRequest) -> QuerySet[ArtifactInventory]:
        # Up to ARTIFACT_SCAN_MAX_ENTRIES rows of JSON; only the detail page reads it.
        return super().get_queryset(request).defer("entries")


@admin.register(Stage0Requirement)
class Stage0RequirementAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[Stage0Requirement]):
    list_display = ("id", "project", "key", "kind", "status", "checked_at")
    list_select_related = ("project",)
    list_filter = ("status", "kind")
    search_fields = ("key", "title")


@admin.register(PreconditionAnalysis)
class PreconditionAnalysisAdmin(
    _ReadOnlyAdminMixin, admin.ModelAdmin[PreconditionAnalysis]
):
    list_display = ("id", "project", "preconditions_md5", "none_needed", "analysed_at")
    list_select_related = ("project",)
