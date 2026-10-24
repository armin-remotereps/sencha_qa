from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from notifications.models import NotificationPreference


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin[NotificationPreference]):
    """Read-only, and never shows the topic (a secret of its owner)."""

    list_display = (
        "id",
        "user",
        "enabled",
        "notify_need_help",
        "notify_failed",
        "created_at",
        "updated_at",
    )
    list_select_related = ("user",)
    search_fields = ("user__email",)
    fields = (
        "user",
        "enabled",
        "notify_need_help",
        "notify_failed",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: NotificationPreference | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: NotificationPreference | None = None
    ) -> bool:
        return False
