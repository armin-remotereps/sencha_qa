from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.exceptions import CheckedPushError, TestRailRunImportError
from projects.forms import (
    ProjectMemberInviteForm,
    ProjectMemberRoleForm,
    ProjectRunModeForm,
    RunCaseSelectionForm,
    TestRailImportStartForm,
    TestRailPushStartForm,
    TestRailRunImportForm,
    TestRailSettingsForm,
    TestRailSuiteForm,
    form_error_text,
)
from projects.models import Project, RunHelpMode, TestRun
from projects.project_access import get_request_project_access
from projects.project_member_services import list_project_members
from projects.services import get_test_run_for_project
from projects.testrail_case_send_services import (
    format_case_send_message,
    list_case_sendable_pivot_ids,
    start_case_send,
)
from projects.testrail_checked_push_services import start_checked_push
from projects.testrail_client import TestRailError, TestRailProject, TestRailSuite
from projects.testrail_results_services import (
    set_testrail_target,
    start_testrail_results_push,
    suggest_testrail_target,
)
from projects.testrail_run_import_services import start_testrail_run_import
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


def render_project_settings(
    request: AuthenticatedRequest,
    project: Project,
    *,
    testrail_form: TestRailSettingsForm | None = None,
    invite_form: ProjectMemberInviteForm | None = None,
    run_mode_form: ProjectRunModeForm | None = None,
) -> HttpResponse:
    """Render the Settings tab; a card whose form is not given gets a fresh one."""
    if testrail_form is None:
        testrail_form = TestRailSettingsForm(
            initial=_settings_initial(project),
            has_stored_key=bool(project.testrail_api_key_encrypted),
        )
    if invite_form is None:
        invite_form = ProjectMemberInviteForm()
    if run_mode_form is None:
        run_mode_form = ProjectRunModeForm(
            initial={"help_mode": project.default_help_mode}
        )
    access = get_request_project_access(request)
    project_can_write = access is not None and access.can_write
    members = list_project_members(project, viewer=request.user)
    return render(
        request,
        "projects/settings.html",
        {
            "project": project,
            "form": testrail_form,
            "api_key_hint": get_testrail_api_key_hint(project),
            "members": members.rows,
            "can_leave": members.can_leave,
            "viewer_can_write": members.viewer_can_write,
            "invite_form": invite_form,
            # Unbound: the Members card renders its role choices per row.
            "role_form": ProjectMemberRoleForm(),
            "run_mode_form": run_mode_form,
            "run_mode_label": RunHelpMode(project.default_help_mode).label,
            "can_change_run_mode": request.user.is_superuser and project_can_write,
            "active_tab": "settings",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_settings(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Settings tab: members card and TestRail integration card."""
    if request.method == "GET":
        return render_project_settings(request, project)

    form = TestRailSettingsForm(
        request.POST, has_stored_key=bool(project.testrail_api_key_encrypted)
    )
    if not form.is_valid():
        return render_project_settings(request, project, testrail_form=form)

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


def _safe_next(request: HttpRequest, next_url: str, default: str) -> str:
    """Return `next_url` when it stays on this host, else `default`."""
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return default


@project_membership_required
@require_POST
def testrail_run_import(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Import the cases of one TestRail run, pasted as a run URL or ID."""
    form = TestRailRunImportForm(request.POST)
    is_valid = form.is_valid()
    target_url = _safe_next(
        request,
        form.cleaned_data.get("next", ""),
        reverse("projects:test_case_import", args=[project.id]),
    )
    if not is_valid:
        messages.error(request, "Paste a TestRail run URL or ID.")
        return redirect(target_url)

    try:
        upload = start_testrail_run_import(
            project=project,
            user=request.user,
            raw_reference=form.cleaned_data["run_reference"],
        )
    except (
        TestRailNotConfiguredError,
        TestRailRunImportError,
        TestRailError,
    ) as exc:
        messages.error(request, exc.message)
        return redirect(target_url)

    messages.success(request, f"Importing {upload.original_filename}.")
    return redirect(target_url)


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
    request: HttpRequest, project: Project, test_run: TestRun
) -> HttpResponse:
    try:
        start_testrail_results_push(test_run=test_run)
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
) -> HttpResponse:
    set_testrail_target(test_run, target)
    return _start_push_and_notify(request, project, test_run)


def _push_with_existing_target(
    request: HttpRequest, project: Project, test_run: TestRun
) -> HttpResponse:
    if not test_run.has_testrail_target:
        messages.error(request, "Choose a TestRail project first.")
        return _redirect_to_run(project, test_run)
    return _start_push_and_notify(request, project, test_run)


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

    form = TestRailPushStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose a TestRail project first.")
        return _redirect_to_run(project, test_run)

    testrail_project_id = form.cleaned_data["testrail_project_id"]
    if testrail_project_id is None:
        return _push_with_existing_target(request, project, test_run)

    try:
        resolution = resolve_testrail_import(project, testrail_project_id)
    except (TestRailError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_run(project, test_run)

    single_suite = resolution.single_suite
    if single_suite is None:
        return _render_push_suite_picker(request, project, test_run, resolution)
    target = TestRailImportTarget(resolution.testrail_project, single_suite)
    return _set_target_and_push(request, project, test_run, target)


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
    return _set_target_and_push(request, project, test_run, target)


@project_membership_required
@require_POST
def testrail_send_checked(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    """Add each checked, unsent case's result to its source TestRail run."""
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        start_checked_push(test_run=test_run)
    except (CheckedPushError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return _redirect_to_run(project, test_run)
    messages.info(request, "Sending checked results to TestRail…")
    return _redirect_to_run(project, test_run)


_CASE_SEND_STALE_MESSAGE = (
    "Some selected cases can no longer be sent. Reload the page and try again."
)


@project_membership_required
@require_POST
def test_run_cases_send_testrail(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    """Check the selected failed cases and send them to their source TestRail run now."""
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    test_run.project = project
    target_url = _safe_next(
        request,
        request.POST.get("next", ""),
        reverse("projects:test_run_detail", args=[project.id, test_run.id]),
    )
    form = RunCaseSelectionForm(list_case_sendable_pivot_ids(test_run), request.POST)
    if not form.is_valid():
        if form.has_error("pivot_ids", "invalid_choice"):
            messages.error(request, _CASE_SEND_STALE_MESSAGE)
        else:
            messages.error(request, form_error_text(form))
        return redirect(target_url)
    try:
        outcome = start_case_send(
            test_run=test_run,
            pivot_ids=form.cleaned_data["pivot_ids"],
            user=request.user,
        )
    except (CheckedPushError, TestRailNotConfiguredError) as exc:
        messages.error(request, exc.message)
        return redirect(target_url)
    messages.info(request, format_case_send_message(outcome))
    return redirect(target_url)
