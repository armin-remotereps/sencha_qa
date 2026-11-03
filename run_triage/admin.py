from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from run_triage.models import RunTriage


@admin.register(RunTriage)
class RunTriageAdmin(admin.ModelAdmin[RunTriage]):
    """Read-only: the analysis is written by its Machine setup thread only."""

    list_display = ("id", "test_run", "last_classified_at", "last_grouped_at")
    readonly_fields = (
        "test_run",
        "category_notes",
        "last_classified_at",
        "last_grouped_at",
        "prompt_version",
        "updated_at",
    )

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RunTriage | None = None
    ) -> bool:
        return False
