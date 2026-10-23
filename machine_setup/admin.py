from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from machine_setup.models import (
    SetupConversation,
    SetupMessage,
    SetupScreenshot,
    SetupTurn,
)


class _ReadOnlyAdminMixin:
    """Setup rows are written only by the chat; admin is for auditing."""

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


@admin.register(SetupConversation)
class SetupConversationAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[SetupConversation]):
    list_display = ("id", "project", "created_by", "created_at", "archived_at")
    list_filter = ("archived_at", "project")
    list_select_related = ("project", "created_by")

    def get_queryset(self, request: HttpRequest) -> QuerySet[SetupConversation]:
        # The transcript can be large; only the detail page reads it.
        return super().get_queryset(request).defer("llm_transcript")


@admin.register(SetupTurn)
class SetupTurnAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[SetupTurn]):
    list_display = (
        "id",
        "project",
        "conversation_id",
        "status",
        "ended_reason",
        "iterations",
        "created_at",
        "finished_at",
    )
    list_filter = ("status", "ended_reason", "project")
    list_select_related = ("project",)


@admin.register(SetupMessage)
class SetupMessageAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[SetupMessage]):
    list_display = (
        "id",
        "conversation_id",
        "turn_id",
        "kind",
        "tool_name",
        "created_at",
    )
    list_filter = ("kind", "tool_status")
    search_fields = ("text", "tool_name")


@admin.register(SetupScreenshot)
class SetupScreenshotAdmin(_ReadOnlyAdminMixin, admin.ModelAdmin[SetupScreenshot]):
    list_display = ("id", "message_id", "tool_name", "created_at")
    list_filter = ("tool_name",)
