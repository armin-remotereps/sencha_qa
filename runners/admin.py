from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from runners.models import TestRunner


@admin.register(TestRunner)
class TestRunnerAdmin(admin.ModelAdmin[TestRunner]):
    """Read-only: runners are created and changed by their owner on ``/runners/``.

    The token fields are never shown.
    """

    list_display = (
        "id",
        "name",
        "owner",
        "connected",
        "client_version",
        "last_connected_at",
        "archived_at",
    )
    list_select_related = ("owner",)
    list_filter = ("connected",)
    search_fields = ("name", "owner__email")
    fields = (
        "owner",
        "name",
        "connected",
        "client_version",
        "capabilities",
        "system_info",
        "device_state",
        "device_detail",
        "device_reported_at",
        "last_connected_at",
        "created_at",
        "updated_at",
        "archived_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: TestRunner | None = None
    ) -> bool:
        return False
