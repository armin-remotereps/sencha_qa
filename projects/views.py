from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from accounts.models import CustomUser
from accounts.types import AuthenticatedRequest
from auto_tester.pagination import (
    ALLOWED_PER_PAGE,
    elided_page_range,
    parse_page,
    parse_per_page,
)
from projects.application_context_services import (
    get_context_provenance,
    list_context_versions,
    restore_context_version,
)
from projects.case_checkpoint_services import list_resume_steps
from projects.case_duration_services import (
    get_case_wall_clock_with_waiting,
    get_run_case_duration_stats,
)
from projects.case_help_services import (
    continue_after_help,
    continue_paused_run,
    format_help_action_message,
    get_help_panel,
    get_open_help_request,
    get_run_case_for_help,
    list_case_help_requests,
    mark_blocked_after_help,
    mark_failed_after_help,
    resolve_return_thread,
    stop_auto_fix,
)
from projects.case_plan_revision_services import (
    discard_plan_revision,
    plan_revision_belongs_to,
)
from projects.case_result_page import get_case_result_panel
from projects.case_step_progress import case_step_view
from projects.decorators import get_writable_project, project_membership_required
from projects.exceptions import (
    ApplicationContextVersionNotFoundError,
    HelpModeNotAllowedError,
    PlanRevisionError,
    RunCaseCheckError,
    RunHelpError,
    RunRerunError,
    RunResumeError,
)
from projects.forms import (
    ApplicationContextForm,
    CaseHelpResolutionForm,
    CaseResumeForm,
    ContextRestoreForm,
    HelpReturnForm,
    PersonStepReportForm,
    ProjectForm,
    RunCaseSelectionForm,
    RunCasesRerunForm,
    RunHelpModeChangeForm,
    RunHelpModeForm,
    TestCaseForm,
    TestRunCreateForm,
    TestRunStartForm,
    form_error_text,
)
from projects.models import (
    PROJECT_PROMPT_MAX_LENGTH,
    Project,
    RunHelpMode,
    TestRun,
    TestRunStatus,
    TestRunTestCase,
)
from projects.person_step_services import get_person_step_panel
from projects.project_access import get_request_project_access
from projects.project_access_services import get_project_access
from projects.run_case_check_services import (
    check_run_cases,
    format_check_message,
    is_checkable,
    is_uncheckable,
    list_checkable_pivot_ids,
    list_uncheckable_pivot_ids,
    uncheck_run_cases,
)
from projects.run_help_mode_services import change_run_help_mode
from projects.run_pause_panel import get_run_pause_panels
from projects.run_rerun_services import (
    format_rerun_message,
    get_rerun_panel,
    list_rerunnable_pivot_ids,
    rerun_test_run_cases,
    rerunnable_ids_on_page,
)
from projects.run_resume_services import (
    can_resume_case,
    get_resume_panel,
    get_run_case_for_resume,
    list_case_resumes,
    resume_test_run,
    resume_test_run_case,
)
from projects.runner_update_offer import get_runner_update_offer
from projects.services import (
    STATUS_FILTER_CHOICES,
    RerunScope,
    RunReadiness,
    abort_test_run,
    add_cases_to_test_run,
    archive_project,
    build_run_narrative,
    bulk_delete_test_cases,
    can_edit_test_case_in_run,
    can_rerun_cases,
    cancel_upload_processing,
    connected_machine_state,
    copy_test_cases_to_project,
    count_test_cases,
    create_project,
    create_test_case,
    create_test_run_with_cases,
    delete_test_case,
    delete_test_run,
    delete_upload,
    duplicate_project,
    force_disconnect_controller,
    generate_controller_client_zip,
    get_all_tags_for_user,
    get_case_position,
    get_latest_runs_by_project,
    get_machine_state,
    get_project_overview,
    get_project_tag_names,
    get_run_case_picker,
    get_run_readiness,
    get_test_case_for_project,
    get_test_run_case_detail,
    get_test_run_for_project,
    get_test_run_summary,
    get_upload_for_project,
    list_blocked_case_titles,
    list_completed_uploads_for_project,
    list_failed_case_titles,
    list_other_projects_for_user,
    list_projects_for_user,
    list_test_cases_for_project,
    list_test_run_cases,
    list_test_runs_for_project,
    list_uploads_for_project,
    list_waiting_test_runs_for_project,
    regenerate_api_key,
    remove_case_from_test_run,
    rerun_cases,
    reset_test_run,
    save_application_context,
    save_project_prompt,
    start_test_run,
    suggest_run_name,
    unarchive_project,
    update_project,
    update_test_case,
)
from projects.tasks import refine_project_prompt_task
from projects.testrail_checked_push_services import (
    get_checked_push_panel,
    get_source_run_url,
)
from projects.testrail_mapping import format_push_confirm_body
from projects.testrail_results_services import get_testrail_push_panel
from projects.testrail_services import get_testrail_picker_state
from visual_engine.services.pool_status import get_visual_engine_status

PROJECTS_DEFAULT_PER_PAGE = 9


def _build_query_params(request: HttpRequest) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"&{encoded}" if encoded else ""


def _parse_test_case_ids(request: HttpRequest) -> list[int]:
    raw_ids = request.POST.getlist("test_case_ids")
    return [int(x) for x in raw_ids if x.isdigit()]


def _safe_next_url(request: HttpRequest, next_url: str, default_url: str) -> str:
    """Return `next_url` when it stays on this host, else `default_url`."""
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return default_url


def _next_or_default(request: HttpRequest, default_url: str) -> str:
    """Resolve a validated `next` POST param, falling back to `default_url`."""
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return next_url
    return default_url


# ============================================================================
# PROJECT LIST / CRUD VIEWS
# ============================================================================


@login_required
def project_list(request: AuthenticatedRequest) -> HttpResponse:
    user = request.user
    search = request.GET.get("search", "").strip() or None
    tag_filter = request.GET.get("tag", "").strip() or None
    show_archived = request.GET.get("archived") == "1"
    page = parse_page(request)
    per_page = parse_per_page(request, PROJECTS_DEFAULT_PER_PAGE)

    projects = list_projects_for_user(
        user=user,
        search=search,
        tag_filter=tag_filter,
        page=page,
        per_page=per_page,
        include_archived=show_archived,
    )
    tags = get_all_tags_for_user(user)
    form = ProjectForm()
    latest_runs = get_latest_runs_by_project(projects)
    # The visual engine is global: one query gives every connected row's state.
    machine_state_if_connected = connected_machine_state(get_visual_engine_status())

    return render(
        request,
        "projects/list.html",
        {
            "projects": projects,
            "latest_runs": latest_runs,
            "connected_machine_state": machine_state_if_connected.value,
            "tags": tags,
            "form": form,
            "search": search or "",
            "current_tag": tag_filter or "",
            "show_archived": show_archived,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": elided_page_range(projects),
            "query_params": _build_query_params(request),
            "active_nav": "projects",
        },
    )


def _render_wizard_details(
    request: HttpRequest, *, project: Project | None, form: ProjectForm
) -> HttpResponse:
    return render(
        request,
        "projects/wizard/step_details.html",
        {
            "project": project,
            "form": form,
            "wizard_step": 1,
            "wizard_mode": "create" if project is None else "edit",
            "active_nav": "projects",
        },
    )


@login_required
def project_create(request: AuthenticatedRequest) -> HttpResponse:
    """Wizard step 1 in create mode: name the project and start the wizard."""
    if request.method == "GET":
        return _render_wizard_details(request, project=None, form=ProjectForm())

    form = ProjectForm(request.POST)
    if not form.is_valid():
        return _render_wizard_details(request, project=None, form=form)

    project = create_project(
        user=request.user,
        name=form.cleaned_data["name"],
        tag_names=form.cleaned_data["tags"],
    )
    return redirect("projects:setup_context", project_id=project.id)


@project_membership_required
def setup_details(request: HttpRequest, project: Project) -> HttpResponse:
    """Wizard step 1 in edit mode ("Restart setup wizard")."""
    if request.method == "GET":
        initial = {
            "name": project.name,
            "tags": ", ".join(get_project_tag_names(project)),
        }
        form = ProjectForm(initial=initial)
        return _render_wizard_details(request, project=project, form=form)

    form = ProjectForm(request.POST)
    if not form.is_valid():
        return _render_wizard_details(request, project=project, form=form)

    update_project(
        project=project,
        name=form.cleaned_data["name"],
        tag_names=form.cleaned_data["tags"],
    )
    return redirect("projects:setup_context", project_id=project.id)


@login_required
@require_POST
def project_edit(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    project = get_writable_project(project_id, request.user)

    form = ProjectForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("projects:list")

    update_project(
        project=project,
        name=form.cleaned_data["name"],
        tag_names=form.cleaned_data["tags"],
    )
    return redirect("projects:list")


@login_required
@require_POST
def project_archive(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    project = get_writable_project(project_id, request.user)

    archive_project(project)
    return redirect("projects:list")


@login_required
@require_POST
def project_restore(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    """Restore an archived project (the lookup includes archived projects)."""
    project = get_writable_project(project_id, request.user, include_archived=True)

    unarchive_project(project)
    messages.success(request, f'"{project.name}" restored.')
    if request.POST.get("archived") == "1":
        return redirect(f"{reverse('projects:list')}?archived=1")
    return redirect("projects:list")


@login_required
@require_POST
def project_duplicate(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    user: CustomUser = request.user
    project = get_writable_project(project_id, user)

    name = request.POST.get("name", "").strip() or f"Copy of {project.name}"
    duplicate_project(source_project=project, user=user, name=name)
    messages.success(request, f'Project duplicated as "{name}".')
    return redirect("projects:list")


# ============================================================================
# PROJECT OVERVIEW / ENVIRONMENT / APPLICATION CONTEXT VIEWS
# ============================================================================


@project_membership_required
def project_overview(request: HttpRequest, project: Project) -> HttpResponse:
    visual_engine = get_visual_engine_status()
    overview = get_project_overview(project, visual_engine)
    open_help_request = (
        get_open_help_request(overview.active_run)
        if overview.active_run is not None
        and overview.active_run.status == TestRunStatus.PAUSED
        else None
    )
    return render(
        request,
        "projects/overview.html",
        {
            "project": project,
            "overview": overview,
            "open_help_request": open_help_request,
            "visual_engine": visual_engine.to_payload(),
            "context_source": get_context_provenance(project),
            "active_tab": "overview",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_environment(request: HttpRequest, project: Project) -> HttpResponse:
    visual_engine = get_visual_engine_status()
    machine_state = get_machine_state(project, visual_engine)
    return render(
        request,
        "projects/environment.html",
        {
            "project": project,
            "machine_state": machine_state,
            "visual_engine": visual_engine.to_payload(),
            "runner_update": get_runner_update_offer(project),
            "active_tab": "environment",
            "active_nav": "projects",
        },
    )


def _application_context_initial(project: Project) -> dict[str, str]:
    return {
        "application_url": project.application_url,
        "application_platform": project.application_platform,
        "project_prompt": project.project_prompt,
    }


def _save_application_context_from_form(
    project: Project, form: ApplicationContextForm, author: CustomUser
) -> None:
    save_application_context(
        project=project,
        author=author,
        application_url=form.cleaned_data["application_url"],
        application_platform=form.cleaned_data["application_platform"],
        project_prompt=form.cleaned_data["project_prompt"],
    )


def _render_application_context_page(
    request: HttpRequest, project: Project, form: ApplicationContextForm
) -> HttpResponse:
    return render(
        request,
        "projects/application_context.html",
        {
            "project": project,
            "form": form,
            "prompt_max_length": PROJECT_PROMPT_MAX_LENGTH,
            "context_versions": list_context_versions(project),
            "context_source": get_context_provenance(project),
            "active_tab": "overview",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_application_context(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Standalone application-context edit page, linked from the overview."""
    if request.method == "GET":
        form = ApplicationContextForm(initial=_application_context_initial(project))
        return _render_application_context_page(request, project, form)

    form = ApplicationContextForm(request.POST)
    if not form.is_valid():
        return _render_application_context_page(request, project, form)

    _save_application_context_from_form(project, form, request.user)
    messages.success(request, "Application context saved.")
    return redirect("projects:detail", project_id=project.id)


@project_membership_required
@require_POST
def application_context_restore(
    request: AuthenticatedRequest, project: Project, version_id: int
) -> HttpResponse:
    """Write an earlier application context version back as a new revision."""
    form = ContextRestoreForm(request.POST)
    next_url = form.cleaned_data["next"] if form.is_valid() else ""
    target_url = _safe_next_url(
        request,
        next_url,
        reverse("projects:application_context", args=[project.id]),
    )
    try:
        revision = restore_context_version(
            project=project, version_id=version_id, user=request.user
        )
    except ApplicationContextVersionNotFoundError:
        raise Http404
    messages.success(request, f"Restored as revision {revision}.")
    return redirect(target_url)


def _render_wizard_context(
    request: HttpRequest, project: Project, form: ApplicationContextForm
) -> HttpResponse:
    return render(
        request,
        "projects/wizard/step_context.html",
        {
            "project": project,
            "form": form,
            "prompt_max_length": PROJECT_PROMPT_MAX_LENGTH,
            "wizard_step": 2,
            "wizard_mode": "edit",
            "active_nav": "projects",
        },
    )


@project_membership_required
def setup_context(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Wizard step 2: application context."""
    if request.method == "GET":
        form = ApplicationContextForm(initial=_application_context_initial(project))
        return _render_wizard_context(request, project, form)

    form = ApplicationContextForm(request.POST)
    if not form.is_valid():
        return _render_wizard_context(request, project, form)

    _save_application_context_from_form(project, form, request.user)
    return redirect("projects:setup_cases", project_id=project.id)


@project_membership_required
@require_POST
def project_regenerate_api_key(request: HttpRequest, project: Project) -> HttpResponse:
    regenerate_api_key(project)
    messages.success(request, "Connection key regenerated successfully.")
    return redirect("projects:environment", project_id=project.id)


@project_membership_required
@require_POST
def download_controller_client(request: HttpRequest, project: Project) -> HttpResponse:
    """Generate and download the Test Runner ZIP for the project."""
    zip_bytes = generate_controller_client_zip(project)
    response = HttpResponse(zip_bytes, content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="controller-client-{project.id}.zip"'
    )
    return response


@project_membership_required
@require_POST
def project_force_disconnect(request: HttpRequest, project: Project) -> HttpResponse:
    if force_disconnect_controller(project):
        messages.success(request, "Test machine disconnected.")
    else:
        messages.info(request, "No test machine is connected.")
    return redirect("projects:environment", project_id=project.id)


# ============================================================================
# TEST CASE VIEWS
# ============================================================================


@project_membership_required
def test_case_list(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    search = request.GET.get("search", "").strip() or None
    upload_filter = request.GET.get("upload", "").strip() or None
    upload_id: int | None = int(upload_filter) if upload_filter else None
    status_filter = request.GET.get("status", "").strip() or None
    page = parse_page(request)
    per_page = parse_per_page(request)

    test_cases = list_test_cases_for_project(
        project=project,
        search=search,
        upload_id=upload_id,
        status_filter=status_filter,
        page=page,
        per_page=per_page,
    )
    form = TestCaseForm()
    completed_uploads = list_completed_uploads_for_project(project=project)
    waiting_test_runs = list_waiting_test_runs_for_project(project)
    user: CustomUser = request.user
    other_projects = list_other_projects_for_user(user=user, exclude_project=project)

    return render(
        request,
        "projects/test_cases.html",
        {
            "project": project,
            "test_cases": test_cases,
            "form": form,
            "search": search or "",
            "uploads": completed_uploads,
            "current_upload": upload_id or "",
            "current_status": status_filter or "",
            "status_choices": STATUS_FILTER_CHOICES,
            "waiting_test_runs": waiting_test_runs,
            "other_projects": other_projects,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": elided_page_range(test_cases),
            "query_params": _build_query_params(request),
            "active_tab": "test_cases",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def test_case_create(request: HttpRequest, project: Project) -> HttpResponse:
    form = TestCaseForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("projects:test_case_list", project_id=project.id)
    create_test_case(project=project, data=form.to_data())
    return redirect("projects:test_case_list", project_id=project.id)


@project_membership_required
@require_POST
def test_case_edit(
    request: HttpRequest, project: Project, test_case_id: int
) -> HttpResponse:
    test_case = get_test_case_for_project(test_case_id, project)
    if test_case is None:
        raise Http404

    form = TestCaseForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("projects:test_case_list", project_id=project.id)
    update_test_case(test_case=test_case, data=form.to_data())
    return redirect("projects:test_case_list", project_id=project.id)


@project_membership_required
@require_POST
def test_case_delete(
    request: HttpRequest, project: Project, test_case_id: int
) -> HttpResponse:
    test_case = get_test_case_for_project(test_case_id, project)
    if test_case is None:
        raise Http404

    delete_test_case(test_case)
    return redirect("projects:test_case_list", project_id=project.id)


@project_membership_required
@require_POST
def test_case_bulk_delete(request: HttpRequest, project: Project) -> HttpResponse:
    test_case_ids = _parse_test_case_ids(request)
    count = bulk_delete_test_cases(project=project, test_case_ids=test_case_ids)
    messages.success(request, f"Deleted {count} test cases.")
    return redirect("projects:test_case_list", project_id=project.id)


@project_membership_required
@require_POST
def test_case_copy_to_project(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    user: CustomUser = request.user
    target_project_id = request.POST.get("target_project_id", "")
    if not target_project_id.isdigit():
        raise Http404
    # The target must be a project the user can change (not read-only there).
    target_access = get_project_access(int(target_project_id), user)
    if target_access is None or not target_access.can_write:
        raise Http404
    target_project = target_access.project

    test_case_ids = _parse_test_case_ids(request)
    if not test_case_ids:
        messages.error(request, "Select at least one test case.")
        return redirect("projects:test_case_list", project_id=project.id)

    count = copy_test_cases_to_project(
        source_project=project,
        target_project=target_project,
        test_case_ids=test_case_ids,
    )
    messages.success(request, f"{count} test case(s) copied to {target_project.name}.")
    return redirect("projects:test_case_list", project_id=target_project.id)


# ============================================================================
# UPLOAD / IMPORT VIEWS
# ============================================================================


@project_membership_required
def test_case_import(request: HttpRequest, project: Project) -> HttpResponse:
    """Display the TestRail import cards and the paginated upload history."""
    page = parse_page(request)
    per_page = parse_per_page(request)
    uploads = list_uploads_for_project(project=project, page=page, per_page=per_page)
    access = get_request_project_access(request)
    # The import cards are hidden from read-only members: skip the TestRail call.
    can_write = access is not None and access.can_write
    return render(
        request,
        "projects/import.html",
        {
            "project": project,
            "uploads": uploads,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": elided_page_range(uploads),
            "query_params": _build_query_params(request),
            "testrail_picker": (
                get_testrail_picker_state(project) if can_write else None
            ),
            "active_tab": "test_cases",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def upload_cancel(
    request: HttpRequest, project: Project, upload_id: int
) -> HttpResponse:
    """Cancel an in-progress upload."""
    upload = get_upload_for_project(upload_id, project)
    if upload is None:
        raise Http404

    cancel_upload_processing(upload)
    return redirect("projects:test_case_import", project_id=project.id)


@project_membership_required
@require_POST
def upload_delete(
    request: HttpRequest, project: Project, upload_id: int
) -> HttpResponse:
    """Delete an upload and its cascaded test cases."""
    upload = get_upload_for_project(upload_id, project)
    if upload is None:
        raise Http404

    delete_upload(upload)
    return redirect("projects:test_case_import", project_id=project.id)


# ============================================================================
# TEST RUN VIEWS
# ============================================================================


@project_membership_required
def test_run_list(request: HttpRequest, project: Project) -> HttpResponse:
    page = parse_page(request)
    per_page = parse_per_page(request)
    test_runs = list_test_runs_for_project(
        project=project, page=page, per_page=per_page
    )
    return render(
        request,
        "projects/test_runs.html",
        {
            "project": project,
            "test_runs": test_runs,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": elided_page_range(test_runs),
            "query_params": _build_query_params(request),
            "active_tab": "runs",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def test_run_create(request: HttpRequest, project: Project) -> HttpResponse:
    form = TestRunCreateForm(project, request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("projects:test_case_list", project_id=project.id)

    test_run = create_test_run_with_cases(
        project=project,
        test_case_ids=form.cleaned_data["test_case_ids"],
        name=form.cleaned_data["name"],
    )
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_add_cases(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    test_case_ids = _parse_test_case_ids(request)
    add_cases_to_test_run(test_run=test_run, test_case_ids=test_case_ids)
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_start(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    form = TestRunStartForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        try:
            start_test_run(
                test_run, user=request.user, help_mode=form.cleaned_data["help_mode"]
            )
        except HelpModeNotAllowedError as exc:
            messages.error(request, exc.message)
        except ValueError as exc:
            messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_abort(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    abort_test_run(test_run)
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_delete(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        delete_test_run(test_run)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(
            "projects:test_run_detail",
            project_id=project.id,
            test_run_id=test_run.id,
        )
    return redirect("projects:test_run_list", project_id=project.id)


@project_membership_required
@require_POST
def test_run_remove_case(
    request: HttpRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        remove_case_from_test_run(test_run, pivot_id)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_reset(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        reset_test_run(test_run)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_rerun_failed(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    return _rerun_test_run_cases(request, project, test_run_id, RerunScope.FAILED)


@project_membership_required
@require_POST
def test_run_rerun_blocked(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    return _rerun_test_run_cases(request, project, test_run_id, RerunScope.BLOCKED)


def _rerun_test_run_cases(
    request: HttpRequest, project: Project, test_run_id: int, scope: RerunScope
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        new_run = rerun_cases(test_run, scope)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(
            "projects:test_run_detail",
            project_id=project.id,
            test_run_id=test_run.id,
        )
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=new_run.id
    )


def _build_test_run_detail_context(
    request: HttpRequest, project: Project, test_run: TestRun
) -> dict[str, Any]:
    page = parse_page(request)
    per_page = parse_per_page(request)
    cases = list_test_run_cases(test_run=test_run, page=page, per_page=per_page)
    summary = get_test_run_summary(test_run)
    failed_titles = list_failed_case_titles(test_run) if summary["failed"] else []
    blocked_titles = list_blocked_case_titles(test_run) if summary["blocked"] else []
    narrative = build_run_narrative(test_run, summary, failed_titles, blocked_titles)
    testrail_push = get_testrail_push_panel(
        test_run, links_base_url=request.build_absolute_uri("/")
    )

    # Shared with Resume, so the reopen gates run once per page.
    rerun_panel = get_rerun_panel(test_run, project)
    # Help request / Human in the loop person step / plain "Continue run".
    pause = get_run_pause_panels(test_run, project)

    context: dict[str, Any] = {
        "project": project,
        "test_run": test_run,
        "cases": cases,
        "summary": summary,
        "case_duration": get_run_case_duration_stats(test_run),
        "per_page": per_page,
        "allowed_per_page": ALLOWED_PER_PAGE,
        "elided_page_range": elided_page_range(cases),
        "query_params": _build_query_params(request),
        "form": TestCaseForm(),
        "can_edit": can_edit_test_case_in_run(test_run),
        "narrative": narrative,
        "can_rerun_failed": can_rerun_cases(test_run, summary["failed"]),
        "can_rerun_blocked": can_rerun_cases(test_run, summary["blocked"]),
        "failed_titles": failed_titles,
        "testrail_push": testrail_push,
        "testrail_push_confirm_body": format_push_confirm_body(
            testrail_push.pending_count, test_run.testrail_run_id or 0
        ),
        "testrail_checked_push": get_checked_push_panel(test_run),
        "resume_panel": get_resume_panel(test_run, blockers=rerun_panel.blockers),
        "checkable_pivot_ids": [p.id for p in cases if is_checkable(p)],
        "uncheckable_pivot_ids": [p.id for p in cases if is_uncheckable(p)],
        "rerunnable_pivot_ids": rerunnable_ids_on_page(test_run, cases),
        "rerun_panel": rerun_panel,
        "help_panel": pause.help_panel,
        "help_state": pause.help_state,
        "person_step_panel": pause.person_step_panel,
        "paused_run_blockers": pause.paused_run_blockers,
        # Resume / Re-run dialogs: pre-set to the run's current mode.
        "help_mode_form": RunHelpModeForm(initial={"help_mode": test_run.help_mode}),
        "active_tab": "runs",
        "active_nav": "projects",
    }
    if test_run.status == TestRunStatus.WAITING:
        context["readiness"] = get_run_readiness(project, test_run=test_run)
        context["start_form"] = TestRunStartForm(
            initial={"help_mode": test_run.help_mode}
        )
    return context


@project_membership_required
def test_run_detail(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    context = _build_test_run_detail_context(request, project, test_run)
    return render(request, "projects/test_run_detail.html", context)


@project_membership_required
def test_run_case_detail(
    request: HttpRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_test_run_case_detail(pivot_id, project)
    if pivot is None or pivot.test_run_id != test_run_id:
        raise Http404
    help_panel = get_help_panel(pivot.test_run, project, pivot_id=pivot.id)
    screenshots = list(pivot.screenshots.all())
    form = TestCaseForm()
    can_edit = can_edit_test_case_in_run(pivot.test_run)
    case_position, case_total = get_case_position(pivot)
    source_run_url = get_source_run_url(pivot, project)
    can_resume = can_resume_case(pivot)
    resume_steps = list_resume_steps(pivot) if can_resume else []
    step_progress, earlier_plans = case_step_view(pivot)
    result_panel = get_case_result_panel(pivot)
    return render(
        request,
        "projects/test_run_case_detail.html",
        {
            "project": project,
            "test_run": pivot.test_run,
            "pivot": pivot,
            "screenshots": screenshots,
            "form": form,
            "can_edit": can_edit,
            "case_position": case_position,
            "case_total": case_total,
            "can_check": is_checkable(pivot),
            "can_uncheck": is_uncheckable(pivot),
            "source_run_url": source_run_url,
            "can_resume": can_resume,
            "resume_steps": resume_steps,
            "resume_form": (
                CaseResumeForm(
                    resume_steps, initial={"help_mode": pivot.test_run.help_mode}
                )
                if resume_steps
                else None
            ),
            "help_mode_form": RunHelpModeForm(
                initial={"help_mode": pivot.test_run.help_mode}
            ),
            "help_panel": help_panel,
            "help_requests": list_case_help_requests(pivot),
            # Human in the loop: the step this case waits on a person for.
            "person_step_panel": get_person_step_panel(
                pivot.test_run, project, pivot=pivot
            ),
            "person_step_form": PersonStepReportForm(),
            # The agent's suggested note is shown, never pre-filled: the
            # Continue note reaches the tester as the person's own words.
            # The draft is only summarised here, so it is not applied by default.
            "help_form": CaseHelpResolutionForm(initial={"use_plan_revision": False}),
            "resumes": list_case_resumes(pivot),
            "step_progress": step_progress,
            "earlier_plans": earlier_plans,
            "wall_clock_with_waiting": get_case_wall_clock_with_waiting(pivot),
            "can_discuss_result": result_panel.can_discuss_result,
            "result_thread_id": result_panel.result_thread_id,
            "result_chat_blocker": result_panel.result_chat_blocker,
            "result_changes": result_panel.result_changes,
            "current_result_change": result_panel.current_result_change,
            "current_result_from_label": result_panel.current_result_from_label,
            "pending_review_redesign": result_panel.pending_review_redesign,
            "active_tab": "runs",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def test_run_resume(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    form = RunHelpModeForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect(
            "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
        )
    try:
        resumed = resume_test_run(
            test_run=test_run,
            user=request.user,
            help_mode=form.cleaned_data["help_mode"],
        )
    except (RunResumeError, HelpModeNotAllowedError) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, f"Resumed {resumed} case{'' if resumed == 1 else 's'}."
        )
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_case_resume(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_resume(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    case_detail = redirect(
        "projects:test_run_case_detail",
        project_id=project.id,
        test_run_id=test_run_id,
        pivot_id=pivot.id,
    )
    from_step: int | None = None
    step_options = list_resume_steps(pivot)
    form: RunHelpModeForm = (
        CaseResumeForm(step_options, request.POST)
        if step_options
        else RunHelpModeForm(request.POST)
    )
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return case_detail
    if step_options:
        from_step = form.cleaned_data["from_step"]
    try:
        resume_test_run_case(
            pivot=pivot,
            user=request.user,
            from_step=from_step,
            help_mode=form.cleaned_data["help_mode"],
        )
    except (RunResumeError, HelpModeNotAllowedError) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Resumed this case.")
    return case_detail


def _change_run_case_checks(
    request: AuthenticatedRequest, project: Project, test_run_id: int, *, check: bool
) -> HttpResponse:
    """Shared body of Check / Uncheck: validate the selection, call the service."""
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    allowed_ids = (
        list_checkable_pivot_ids(test_run)
        if check
        else list_uncheckable_pivot_ids(test_run)
    )
    form = RunCaseSelectionForm(allowed_ids, request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        pivot_ids: list[int] = form.cleaned_data["pivot_ids"]
        try:
            if check:
                outcome = check_run_cases(
                    test_run=test_run, pivot_ids=pivot_ids, user=request.user
                )
            else:
                outcome = uncheck_run_cases(test_run=test_run, pivot_ids=pivot_ids)
        except RunCaseCheckError as exc:
            messages.error(request, exc.message)
        else:
            verb = "Checked" if check else "Unchecked"
            messages.success(request, format_check_message(outcome, verb=verb))
    fallback_url = reverse(
        "projects:test_run_detail",
        kwargs={"project_id": project.id, "test_run_id": test_run.id},
    )
    return redirect(_next_or_default(request, fallback_url))


@project_membership_required
@require_POST
def test_run_cases_check(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    return _change_run_case_checks(request, project, test_run_id, check=True)


@project_membership_required
@require_POST
def test_run_cases_uncheck(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    return _change_run_case_checks(request, project, test_run_id, check=False)


@project_membership_required
@require_POST
def test_run_cases_rerun(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    form = RunCasesRerunForm(list_rerunnable_pivot_ids(test_run), request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        pivot_ids: list[int] = form.cleaned_data["pivot_ids"]
        try:
            outcome = rerun_test_run_cases(
                test_run=test_run,
                pivot_ids=pivot_ids,
                user=request.user,
                help_mode=form.cleaned_data["help_mode"],
            )
        except (RunRerunError, HelpModeNotAllowedError) as exc:
            messages.error(request, exc.message)
        else:
            messages.success(request, format_rerun_message(outcome))
    fallback_url = reverse(
        "projects:test_run_detail",
        kwargs={"project_id": project.id, "test_run_id": test_run.id},
    )
    return redirect(_next_or_default(request, fallback_url))


@project_membership_required
@require_POST
def test_run_case_edit(
    request: HttpRequest,
    project: Project,
    test_run_id: int,
    test_case_id: int,
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    if not can_edit_test_case_in_run(test_run):
        return HttpResponseForbidden()
    test_case = get_test_case_for_project(test_case_id, project)
    if test_case is None:
        raise Http404
    if not test_run.pivot_entries.filter(test_case=test_case).exists():
        raise Http404
    form = TestCaseForm(request.POST)
    if form.is_valid():
        update_test_case(test_case=test_case, data=form.to_data())
    else:
        messages.error(request, form_error_text(form))
    fallback_url = reverse(
        "projects:test_run_detail",
        kwargs={"project_id": project.id, "test_run_id": test_run.id},
    )
    return redirect(_next_or_default(request, fallback_url))


# ============================================================================
# NEED HELP: HUMAN ACTIONS ON A PAUSED RUN
# ============================================================================


def _help_case_or_404(
    project: Project, test_run_id: int, pivot_id: int
) -> TestRunTestCase:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    return pivot


def _case_detail_redirect(project: Project, pivot: TestRunTestCase) -> HttpResponse:
    return redirect(
        "projects:test_run_case_detail",
        project_id=project.id,
        test_run_id=pivot.test_run_id,
        pivot_id=pivot.id,
    )


def _help_return_redirect(
    project: Project,
    pivot: TestRunTestCase,
    form: CaseHelpResolutionForm | HelpReturnForm,
) -> HttpResponse:
    """Back to the help-chat thread the action came from, else the case page."""
    raw_thread_id = (
        form.cleaned_data.get("return_thread_id") if form.is_valid() else None
    )
    thread_id = resolve_return_thread(
        pivot=pivot, thread_id=raw_thread_id if isinstance(raw_thread_id, int) else None
    )
    if thread_id is None:
        return _case_detail_redirect(project, pivot)
    return redirect(
        "machine_setup:thread", project_id=project.id, conversation_id=thread_id
    )


@project_membership_required
@require_POST
def test_run_case_help_continue(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _help_case_or_404(project, test_run_id, pivot_id)
    form = CaseHelpResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_detail_redirect(project, pivot)
    try:
        result = continue_after_help(
            pivot=pivot,
            user=request.user,
            note=form.cleaned_data["note"],
            plan_revision_id=form.cleaned_data["plan_revision_id"],
            use_plan_revision=form.cleaned_data["use_plan_revision"],
        )
    except RunHelpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, format_help_action_message("Continuing", result))
    return _help_return_redirect(project, pivot, form)


@project_membership_required
@require_POST
def test_run_case_help_fail(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _help_case_or_404(project, test_run_id, pivot_id)
    form = CaseHelpResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_detail_redirect(project, pivot)
    try:
        result = mark_failed_after_help(
            pivot=pivot, user=request.user, note=form.cleaned_data["note"]
        )
    except RunHelpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, format_help_action_message("Marked the case failed", result)
        )
    return _help_return_redirect(project, pivot, form)


@project_membership_required
@require_POST
def test_run_case_help_block(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _help_case_or_404(project, test_run_id, pivot_id)
    form = CaseHelpResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_detail_redirect(project, pivot)
    try:
        result = mark_blocked_after_help(
            pivot=pivot, user=request.user, note=form.cleaned_data["note"]
        )
    except RunHelpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, format_help_action_message("Marked the case blocked", result)
        )
    return _help_return_redirect(project, pivot, form)


@project_membership_required
@require_POST
def test_run_case_plan_revision_discard(
    request: AuthenticatedRequest,
    project: Project,
    test_run_id: int,
    pivot_id: int,
    revision_id: int,
) -> HttpResponse:
    pivot = _help_case_or_404(project, test_run_id, pivot_id)
    if not plan_revision_belongs_to(pivot=pivot, revision_id=revision_id):
        raise Http404
    form = HelpReturnForm(request.POST)
    try:
        discard_plan_revision(pivot=pivot, revision_id=revision_id, user=request.user)
    except PlanRevisionError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            "Discarded the redesigned plan. Continue now re-runs the step that "
            "needed help.",
        )
    return _help_return_redirect(project, pivot, form)


@project_membership_required
@require_POST
def test_run_case_help_stop_auto_fix(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _help_case_or_404(project, test_run_id, pivot_id)
    try:
        stop_auto_fix(pivot=pivot, user=request.user)
    except RunHelpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, "Stopping the auto-fix. The case now waits for a person."
        )
    return _case_detail_redirect(project, pivot)


@project_membership_required
@require_POST
def test_run_continue(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        continue_paused_run(test_run=test_run, user=request.user)
    except RunHelpError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "The run continues.")
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
@require_POST
def test_run_help_mode(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    run_page = redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )
    form = RunHelpModeChangeForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return run_page
    try:
        change = change_run_help_mode(
            test_run=test_run,
            user=request.user,
            help_mode=form.cleaned_data["help_mode"],
        )
    except (RunHelpError, HelpModeNotAllowedError) as exc:
        messages.error(request, exc.message)
        return run_page
    label = RunHelpMode(change.help_mode).label
    if change.handed_back:
        prefix = f"Help mode changed to {label}. " if change.changed else f"{label}: "
        if change.blocker:
            messages.warning(
                request,
                f"{prefix}The agent performs the waiting step itself, but the "
                f"run could not continue: {change.blocker} Use Continue run "
                "once that is resolved.",
            )
        else:
            messages.success(
                request, f"{prefix}The agent now performs the waiting step itself."
            )
    elif not change.changed:
        messages.info(request, f"The help mode already is {label}.")
    elif change.auto_fix_started:
        messages.success(
            request,
            f"Help mode changed to {label}. The Machine setup agent now tries to "
            "fix the case that needs help.",
        )
    else:
        messages.success(
            request,
            f"Help mode changed to {label}. It applies from the next case that "
            "needs help.",
        )
    return run_page


# ============================================================================
# ONBOARDING WIZARD — STEPS 3-5
# ============================================================================


@project_membership_required
@require_GET
def setup_cases(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Wizard step 3: import test cases."""
    uploads = list_uploads_for_project(project=project, page=1, per_page=5)
    return render(
        request,
        "projects/wizard/step_cases.html",
        {
            "project": project,
            "wizard_step": 3,
            "wizard_mode": "edit",
            "uploads": uploads,
            "case_count": count_test_cases(project),
            "active_nav": "projects",
        },
    )


@project_membership_required
def setup_machine(request: HttpRequest, project: Project) -> HttpResponse:
    """Wizard step 4: connect a test machine."""
    return render(
        request,
        "projects/wizard/step_machine.html",
        {
            "project": project,
            "visual_engine": get_visual_engine_status().to_payload(),
            "wizard_step": 4,
            "wizard_mode": "edit",
            "active_nav": "projects",
        },
    )


def _render_wizard_run(
    request: HttpRequest,
    project: Project,
    form: TestRunCreateForm,
    readiness: RunReadiness,
    selected_ids: frozenset[int] = frozenset(),
) -> HttpResponse:
    picker = get_run_case_picker(project)
    return render(
        request,
        "projects/wizard/step_run.html",
        {
            "project": project,
            "wizard_step": 5,
            "wizard_mode": "edit",
            "form": form,
            "test_cases": picker.test_cases,
            "total_case_count": picker.total_count,
            "readiness": readiness,
            "selected_ids": selected_ids,
            "active_nav": "projects",
        },
    )


RUN_BLOCKED_MESSAGE = "This run can't start yet — resolve the failed checks below."


def _handle_setup_run_submit(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    form = TestRunCreateForm(project, request.POST)
    selected_ids = frozenset(_parse_test_case_ids(request))
    if not form.is_valid():
        readiness = get_run_readiness(project, selected_count=len(selected_ids))
        return _render_wizard_run(request, project, form, readiness, selected_ids)

    test_case_ids: list[int] = form.cleaned_data["test_case_ids"]
    readiness = get_run_readiness(project, selected_count=len(test_case_ids))
    if readiness.blockers:
        messages.error(request, RUN_BLOCKED_MESSAGE)
        return _render_wizard_run(request, project, form, readiness, selected_ids)

    test_run = create_test_run_with_cases(
        project=project,
        test_case_ids=test_case_ids,
        name=form.cleaned_data["name"],
    )
    try:
        # The wizard has no mode choice: a first run is attended.
        start_test_run(test_run, user=request.user, help_mode=RunHelpMode.ATTENDED)
    except HelpModeNotAllowedError as exc:
        messages.error(request, exc.message)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
def setup_run(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Wizard step 5: pick cases, name the run, and start it."""
    if request.method == "POST":
        return _handle_setup_run_submit(request, project)

    form = TestRunCreateForm(project, initial={"name": suggest_run_name(project)})
    readiness = get_run_readiness(project, selected_count=0)
    return _render_wizard_run(request, project, form, readiness)


# ============================================================================
# PROJECT PROMPT (LEGACY "REFINE WITH AI") VIEWS
# ============================================================================


@project_membership_required
@require_POST
def project_save_prompt(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    prompt = request.POST.get("project_prompt", "")[:PROJECT_PROMPT_MAX_LENGTH]
    save_project_prompt(project=project, prompt=prompt, author=request.user)
    messages.success(request, "Project prompt saved.")
    return redirect("projects:detail", project_id=project.id)


@project_membership_required
@require_POST
def project_refine_prompt(request: HttpRequest, project: Project) -> HttpResponse:
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    raw_prompt = str(body.get("prompt", ""))[:PROJECT_PROMPT_MAX_LENGTH]
    if not raw_prompt.strip():
        return JsonResponse({"error": "Prompt is empty."}, status=400)
    refine_project_prompt_task.delay(project.id, raw_prompt)
    return JsonResponse({"status": "accepted"}, status=202)
