from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from visual_engine.models import VisionWorker


@admin.register(VisionWorker)
class VisionWorkerAdmin(admin.ModelAdmin[VisionWorker]):
    """Read-only: rows are written only by the vision worker consumer."""

    list_display = (
        "name",
        "connected",
        "state",
        "device",
        "client_version",
        "connected_at",
        "last_seen_at",
        "last_disconnected_at",
    )
    list_filter = ("connected", "state")
    search_fields = ("name",)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: VisionWorker | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: VisionWorker | None = None
    ) -> bool:
        return False
