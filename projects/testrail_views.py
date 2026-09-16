from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.forms import (
    TestRailImportStartForm,
    TestRailSettingsForm,
    TestRailSuiteForm,
)
from projects.models import Project
from projects.testrail_client import TestRailError
from projects.testrail_services import (
    TestRailImportResolution,
    TestRailImportTarget,
    TestRailNotConfiguredError,
    check_testrail_connection,
    clear_testrail_settings,
    get_testrail_api_key_hint,
    resolve_testrail_import,
    resolve_testrail_import_suite,
    save_testrail_settings,
    start_testrail_import,
)


def _settings_initial(project: Project) -> dict[str, str]:
    return {
        "testrail_url": project.testrail_url,
        "testrail_email": project.testrail_email,
    }


def _render_settings_page(
    request: HttpRequest, project: Project, form: TestRailSettingsForm
) -> HttpResponse:
    return render(
        request,
        "projects/settings.html",
        {
            "project": project,
            "form": form,
            "api_key_hint": get_testrail_api_key_hint(project),
            "active_tab": "settings",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_settings(request: HttpRequest, project: Project) -> HttpResponse:
    """Settings tab: TestRail integration card."""
    has_stored_key = bool(project.testrail_api_key_encrypted)
    if request.method == "GET":
        form = TestRailSettingsForm(
            initial=_settings_initial(project), has_stored_key=has_stored_key
        )
        return _render_settings_page(request, project, form)

    form = TestRailSettingsForm(request.POST, has_stored_key=has_stored_key)
    if not form.is_valid():
        return _render_settings_page(request, project, form)

    save_testrail_settings(
        project=project,
        url=form.cleaned_data["testrail_url"],
        email=form.cleaned_data["testrail_email"],
        api_key=form.cleaned_data["testrail_api_key"],
    )
    messages.success(request, "TestRail settings saved.")
    return redirect("projects:settings", project_id=project.id)


@project_membership_required
@require_POST
def settings_testrail_test(request: HttpRequest, project: Project) -> HttpResponse:
    """Try connecting to TestRail with the stored settings and report the result."""
    result = check_testrail_connection(project)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.error(request, result.message)
    return redirect("projects:settings", project_id=project.id)


@project_membership_required
@require_POST
def settings_testrail_clear(request: HttpRequest, project: Project) -> HttpResponse:
    """Remove the stored TestRail URL, email, and API key from the project."""
    clear_testrail_settings(project)
    messages.success(request, "TestRail settings cleared.")
    return redirect("projects:settings", project_id=project.id)


def _redirect_to_import(project: Project) -> HttpResponse:
    return redirect("projects:test_case_import", project_id=project.id)


def _render_suite_picker(
    request: HttpRequest, project: Project, resolution: TestRailImportResolution
) -> HttpResponse:
    return render(
        request,
        "projects/testrail_suite_picker.html",
        {
            "project": project,
            "testrail_project": resolution.testrail_project,
            "suites": resolution.suites,
            "active_tab": "test_cases",
            "active_nav": "projects",
        },
    )


def _start_and_notify(
    request: AuthenticatedRequest, project: Project, target: TestRailImportTarget
) -> HttpResponse:
    start_testrail_import(project=project, user=request.user, target=target)
    messages.success(
        request,
        f"Importing “{target.suite.name}” from TestRail project “{target.testrail_project.name}”.",
    )
    return _redirect_to_import(project)


@project_membership_required
@require_POST
def testrail_import_start(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Step 1: TestRail project chosen. Single suite → start; several → pick a suite."""
    form = TestRailImportStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a TestRail project to import from.")
        return _redirect_to_import(project)

    try:
        resolution = resolve_testrail_import(
            project, form.cleaned_data["testrail_project_id"]
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_import(project)

    single_suite = resolution.single_suite
    if single_suite is None:
        return _render_suite_picker(request, project, resolution)
    return _start_and_notify(
        request,
        project,
        TestRailImportTarget(resolution.testrail_project, single_suite),
    )


@project_membership_required
@require_POST
def testrail_import_suite(
    request: AuthenticatedRequest, project: Project, testrail_project_id: int
) -> HttpResponse:
    """Step 2 (multi-suite projects): suite chosen → start the import."""
    form = TestRailSuiteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a suite to import.")
        return _redirect_to_import(project)

    try:
        target = resolve_testrail_import_suite(
            project, testrail_project_id, form.cleaned_data["testrail_suite_id"]
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_import(project)
    return _start_and_notify(request, project, target)
