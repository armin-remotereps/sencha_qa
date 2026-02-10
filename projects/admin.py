from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from projects.models import Project, Tag


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    list_display = ("name", "archived", "created_at")
    list_filter = ("archived", "tags")
    filter_horizontal = ("tags", "members")
    actions = ("archive_projects", "unarchive_projects")

    @admin.action(description="Archive selected projects")
    def archive_projects(
        self, request: HttpRequest | None, queryset: QuerySet[Project]
    ) -> None:
        queryset.update(archived=True)

    @admin.action(description="Unarchive selected projects")
    def unarchive_projects(
        self, request: HttpRequest | None, queryset: QuerySet[Project]
    ) -> None:
        queryset.update(archived=False)
