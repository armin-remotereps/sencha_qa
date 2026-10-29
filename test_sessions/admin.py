from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from test_sessions.models import TestSession, TestSessionCase


class TestSessionCaseInline(admin.TabularInline[TestSessionCase, TestSession]):
    model = TestSessionCase
    extra = 0
    can_delete = False
    fields = (
        "project_name",
        "run_name",
        "case_title",
        "testrail_case_id",
        "first_activity",
        "last_activity",
        "activity_count",
        "person_step_count",
        "frozen_at",
        "frozen_status",
    )
    readonly_fields = fields

    def has_add_permission(
        self, request: HttpRequest, obj: TestSession | None = None
    ) -> bool:
        return False


@admin.register(TestSession)
class TestSessionAdmin(admin.ModelAdmin[TestSession]):
    """Read-only: sessions are recorded by their owner's actions only."""

    list_display = (
        "id",
        "user",
        "name",
        "started_at",
        "ended_at",
        "closed_automatically",
    )
    list_select_related = ("user",)
    list_filter = ("closed_automatically",)
    search_fields = ("user__email", "name")
    fields = (
        "user",
        "name",
        "note",
        "started_at",
        "ended_at",
        "closed_automatically",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    inlines = [TestSessionCaseInline]

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: TestSession | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: TestSession | None = None
    ) -> bool:
        return False
