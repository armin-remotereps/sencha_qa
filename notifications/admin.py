from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from notifications.models import MissedFailure, MissedReply, NotificationPreference


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin[NotificationPreference]):
    """Read-only, and never shows the topic (a secret of its owner)."""

    list_display = (
        "id",
        "user",
        "enabled",
        "notify_need_help",
        "notify_failed",
        "notify_night_digest",
        "notify_run_finished",
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
        "notify_night_digest",
        "notify_run_finished",
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


@admin.register(MissedFailure)
class MissedFailureAdmin(admin.ModelAdmin[MissedFailure]):
    """Read-only view of the attention inbox's missed failures."""

    list_display = (
        "id",
        "user",
        "test_run_test_case_id",
        "occurred_at",
        "dismissed_at",
    )
    list_select_related = ("user",)
    search_fields = ("user__email",)
    fields = ("user", "test_run_test_case", "occurred_at", "dismissed_at")
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: MissedFailure | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: MissedFailure | None = None
    ) -> bool:
        return False


@admin.register(MissedReply)
class MissedReplyAdmin(admin.ModelAdmin[MissedReply]):
    """Read-only view of the attention inbox's missed setup replies."""

    list_display = (
        "id",
        "user",
        "conversation_id",
        "outcome",
        "occurred_at",
        "dismissed_at",
    )
    list_select_related = ("user",)
    list_filter = ("outcome",)
    search_fields = ("user__email",)
    fields = (
        "user",
        "conversation",
        "outcome",
        "preview",
        "occurred_at",
        "dismissed_at",
    )
    readonly_fields = fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: MissedReply | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: MissedReply | None = None
    ) -> bool:
        return False
