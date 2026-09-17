from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.forms import (
    TestRailImportStartForm,
    TestRailPushStartForm,
    TestRailSettingsForm,
    TestRailSuiteForm,
)
from projects.models import Project, TestRun
from projects.services import get_test_run_for_project
from projects.testrail_client import TestRailError, TestRailProject, TestRailSuite
from projects.testrail_results_services import (
    set_testrail_target,
    start_testrail_results_push,
    suggest_testrail_target,
)
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


def _render_testrail_suite_picker(
    request: HttpRequest,
    *,
    project: Project,
    testrail_project: TestRailProject,
    suites: list[TestRailSuite],
    form_action: str,
    submit_label: str,
    cancel_url: str,
    intro: str,
    parent_label: str,
    parent_url: str,
    suggested_suite_id: int | None,
    active_tab: str,
    active_nav: str,
) -> HttpResponse:
    """Render the suite picker shared by the import and push-to-TestRail flows."""
    return render(
        request,
        "projects/testrail_suite_picker.html",
        {
            "project": project,
            "testrail_project": testrail_project,
            "suites": suites,
            "form_action": form_action,
            "submit_label": submit_label,
            "cancel_url": cancel_url,
            "intro": intro,
            "parent_label": parent_label,
            "parent_url": parent_url,
            "suggested_suite_id": suggested_suite_id,
            "active_tab": active_tab,
            "active_nav": active_nav,
        },
    )


def _render_suite_picker(
    request: HttpRequest, project: Project, resolution: TestRailImportResolution
) -> HttpResponse:
    import_url = reverse("projects:test_case_import", args=[project.id])
    testrail_project = resolution.testrail_project
    return _render_testrail_suite_picker(
        request,
        project=project,
        testrail_project=testrail_project,
        suites=resolution.suites,
        form_action=reverse(
            "projects:testrail_import_suite", args=[project.id, testrail_project.id]
        ),
        submit_label="Import",
        cancel_url=import_url,
        intro=(
            f"“{testrail_project.name}” has {len(resolution.suites)} suites. "
            "Pick the one to import."
        ),
        parent_label="Import",
        parent_url=import_url,
        suggested_suite_id=None,
        active_tab="test_cases",
        active_nav="projects",
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


def _redirect_to_run(project: Project, test_run: TestRun) -> HttpResponse:
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


def _push_error_message(
    exc: ValueError | TestRailError | TestRailNotConfiguredError,
) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    return exc.message


def _start_push_and_notify(
    request: HttpRequest, project: Project, test_run: TestRun, links_base_url: str
) -> HttpResponse:
    try:
        start_testrail_results_push(test_run=test_run, links_base_url=links_base_url)
    except (ValueError, TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, _push_error_message(exc))
        return _redirect_to_run(project, test_run)
    messages.success(request, "Pushing results to TestRail…")
    return _redirect_to_run(project, test_run)


def _set_target_and_push(
    request: HttpRequest,
    project: Project,
    test_run: TestRun,
    target: TestRailImportTarget,
    links_base_url: str,
) -> HttpResponse:
    set_testrail_target(test_run, target)
    return _start_push_and_notify(request, project, test_run, links_base_url)


def _push_with_existing_target(
    request: HttpRequest, project: Project, test_run: TestRun, links_base_url: str
) -> HttpResponse:
    if not test_run.has_testrail_target:
        messages.error(request, "Choose a TestRail project first.")
        return _redirect_to_run(project, test_run)
    return _start_push_and_notify(request, project, test_run, links_base_url)


def _render_push_suite_picker(
    request: HttpRequest,
    project: Project,
    test_run: TestRun,
    resolution: TestRailImportResolution,
) -> HttpResponse:
    testrail_project = resolution.testrail_project
    run_detail_url = reverse("projects:test_run_detail", args=[project.id, test_run.id])
    suggested_project_id, suggested_suite_id = suggest_testrail_target(test_run)
    if suggested_project_id != testrail_project.id:
        suggested_suite_id = None
    return _render_testrail_suite_picker(
        request,
        project=project,
        testrail_project=testrail_project,
        suites=resolution.suites,
        form_action=reverse(
            "projects:testrail_push_suite",
            args=[project.id, test_run.id, testrail_project.id],
        ),
        submit_label="Push results",
        cancel_url=run_detail_url,
        intro=(
            f"“{testrail_project.name}” has {len(resolution.suites)} suites. "
            "Pick the suite that holds this run's cases."
        ),
        parent_label=test_run.display_name,
        parent_url=run_detail_url,
        suggested_suite_id=suggested_suite_id,
        active_tab="runs",
        active_nav="projects",
    )


@project_membership_required
@require_POST
def testrail_push_start(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    """Step 1: push with the run's existing target, or pick a TestRail project."""
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    links_base_url = request.build_absolute_uri("/")

    form = TestRailPushStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a TestRail project first.")
        return _redirect_to_run(project, test_run)

    testrail_project_id = form.cleaned_data["testrail_project_id"]
    if testrail_project_id is None:
        return _push_with_existing_target(request, project, test_run, links_base_url)

    try:
        resolution = resolve_testrail_import(project, testrail_project_id)
    except (TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_run(project, test_run)

    single_suite = resolution.single_suite
    if single_suite is None:
        return _render_push_suite_picker(request, project, test_run, resolution)
    target = TestRailImportTarget(resolution.testrail_project, single_suite)
    return _set_target_and_push(request, project, test_run, target, links_base_url)


@project_membership_required
@require_POST
def testrail_push_suite(
    request: AuthenticatedRequest,
    project: Project,
    test_run_id: int,
    testrail_project_id: int,
) -> HttpResponse:
    """Step 2 (multi-suite projects): suite chosen → set target and push."""
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    links_base_url = request.build_absolute_uri("/")

    form = TestRailSuiteForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a suite to push results to.")
        return _redirect_to_run(project, test_run)

    try:
        target = resolve_testrail_import_suite(
            project, testrail_project_id, form.cleaned_data["testrail_suite_id"]
        )
    except (TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_run(project, test_run)
    return _set_target_and_push(request, project, test_run, target, links_base_url)
