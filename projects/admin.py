from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from projects.models import (
    ApplicationContextVersion,
    CaseHelpRequest,
    Project,
    ProjectMembership,
    RunnerUpdate,
    Tag,
    TestCase,
    TestCaseUpload,
    TestRun,
    TestRunScreenshot,
    TestRunStatus,
    TestRunTestCase,
    TestRunTestCaseStatus,
)


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin[Tag]):
    list_display = ("name",)
    search_fields = ("name",)


class ProjectMembershipInline(admin.TabularInline[ProjectMembership, Project]):
    model = ProjectMembership
    fields = ("user", "role")
    autocomplete_fields = ("user",)
    extra = 0


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin[Project]):
    list_display = (
        "name",
        "archived",
        "agent_connected",
        "application_platform",
        "created_at",
    )
    list_filter = ("archived", "agent_connected", "application_platform", "tags")
    filter_horizontal = ("tags",)
    inlines = (ProjectMembershipInline,)
    readonly_fields = (
        "api_key",
        "agent_connected",
        "agent_system_info",
        "last_connected_at",
    )
    fields = (
        "name",
        "tags",
        "archived",
        "application_url",
        "application_platform",
        "project_prompt",
        "testrail_url",
        "testrail_email",
        "api_key",
        "agent_connected",
        "agent_system_info",
        "last_connected_at",
    )
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


@admin.register(TestCase)
class TestCaseAdmin(admin.ModelAdmin[TestCase]):
    list_display = (
        "title",
        "project",
        "type",
        "priority",
        "is_converted",
        "created_at",
    )
    list_filter = ("type", "priority", "is_converted", "project")
    search_fields = ("title", "testrail_id")


@admin.register(TestCaseUpload)
class TestCaseUploadAdmin(admin.ModelAdmin[TestCaseUpload]):
    list_display = (
        "original_filename",
        "source",
        "project",
        "uploaded_by",
        "status",
        "total_cases",
        "processed_cases",
        "testrail_run_id",
        "created_at",
    )
    list_filter = ("status", "source", "project")
    search_fields = ("original_filename",)


class TestRunTestCaseInline(admin.TabularInline[TestRunTestCase, TestRun]):
    model = TestRunTestCase
    extra = 1
    readonly_fields = ("status", "result", "logs")


@admin.register(TestRun)
class TestRunAdmin(admin.ModelAdmin[TestRun]):
    list_display = (
        "id",
        "display_name",
        "project",
        "status",
        "started_at",
        "finished_at",
        "created_at",
    )
    list_filter = ("status", "project")
    readonly_fields = ("status",)
    inlines = [TestRunTestCaseInline]

    @admin.display(description="Name")
    def display_name(self, obj: TestRun) -> str:
        return obj.display_name


class TestRunScreenshotInline(admin.TabularInline[TestRunScreenshot, TestRunTestCase]):
    model = TestRunScreenshot
    extra = 0
    readonly_fields = (
        "image",
        "tool_name",
        "created_at",
        "step_num",
        "attempt_id",
        "testrail_evidence",
    )


@admin.register(TestRunTestCase)
class TestRunTestCaseAdmin(admin.ModelAdmin[TestRunTestCase]):
    list_display = (
        "id",
        "test_run",
        "test_case",
        "status",
        "started_at",
        "finished_at",
        "active_duration",
        "created_at",
    )
    list_filter = ("status",)
    readonly_fields = (
        "status",
        "failed_by",
        "result",
        "logs",
        "segment_started_at",
        "active_duration",
        "testrail_source_run_id",
        "checked_at",
        "checked_by",
        "testrail_source_result_id",
        "testrail_source_sent_at",
        "testrail_source_attachment_count",
        "credited_user",
    )
    inlines = [TestRunScreenshotInline]


@admin.register(TestRunScreenshot)
class TestRunScreenshotAdmin(admin.ModelAdmin[TestRunScreenshot]):
    list_display = ("id", "test_run_test_case", "tool_name", "created_at")
    list_filter = ("tool_name",)
    readonly_fields = (
        "image",
        "tool_name",
        "created_at",
        "step_num",
        "attempt_id",
        "testrail_evidence",
    )


@admin.register(ApplicationContextVersion)
class ApplicationContextVersionAdmin(admin.ModelAdmin[ApplicationContextVersion]):
    """Read-only: versions are written by the context service, restored in the app."""

    list_display = (
        "id",
        "project",
        "revision",
        "written_by_source",
        "written_by",
        "written_at",
        "replaced_at",
    )
    list_filter = ("written_by_source", "project")
    list_select_related = ("project", "written_by")
    search_fields = ("text", "change_summary")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: ApplicationContextVersion | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: ApplicationContextVersion | None = None
    ) -> bool:
        return False


@admin.register(CaseHelpRequest)
class CaseHelpRequestAdmin(admin.ModelAdmin[CaseHelpRequest]):
    """Read-only: help requests are written by the run and its help actions."""

    list_display = (
        "id",
        "test_run_test_case",
        "step_num",
        "auto_fix_state",
        "resolution",
        "resolved_by",
        "requested_at",
        "resolved_at",
    )
    list_filter = ("auto_fix_state", "resolution")
    list_select_related = ("test_run_test_case__test_case", "resolved_by")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: CaseHelpRequest | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: CaseHelpRequest | None = None
    ) -> bool:
        return False


@admin.register(RunnerUpdate)
class RunnerUpdateAdmin(admin.ModelAdmin[RunnerUpdate]):
    """Read-only audit: updates are written by the update services."""

    list_display = (
        "id",
        "project",
        "from_version",
        "to_version",
        "status",
        "phase",
        "old_removal",
        "requested_by",
        "created_at",
        "finished_at",
    )
    list_filter = ("status", "old_removal")
    list_select_related = ("project", "requested_by")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: RunnerUpdate | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: RunnerUpdate | None = None
    ) -> bool:
        return False
