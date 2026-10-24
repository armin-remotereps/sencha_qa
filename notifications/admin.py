from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from notifications.models import PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin[PushSubscription]):
    """Read-only, and never shows the endpoint or keys (device secrets)."""

    list_display = ("id", "user", "user_agent", "created_at", "last_success_at")
    list_select_related = ("user",)
    search_fields = ("user__email",)
    fields = ("user", "user_agent", "created_at", "last_success_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: PushSubscription | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: PushSubscription | None = None
    ) -> bool:
        return False
