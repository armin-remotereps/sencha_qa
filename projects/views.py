from __future__ import annotations

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Page
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
from django.views.decorators.http import require_POST

from accounts.models import CustomUser
from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.forms import (
    PROJECT_PROMPT_MAX_LENGTH,
    ApplicationContextForm,
    ProjectForm,
    TestCaseForm,
    TestRunCreateForm,
    form_error_text,
)
from projects.models import Project, TestRun, TestRunStatus
from projects.services import (
    STATUS_FILTER_CHOICES,
    RunReadiness,
    abort_test_run,
    add_cases_to_test_run,
    archive_project,
    build_run_narrative,
    bulk_delete_test_cases,
    can_edit_test_case_in_run,
    can_rerun_failed_cases,
    cancel_upload_processing,
    copy_test_cases_to_project,
    count_test_cases,
    create_project,
    create_test_case,
    create_test_run_with_cases,
    create_upload,
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
    get_project_by_id,
    get_project_for_user,
    get_project_overview,
    get_project_tag_names,
    get_run_case_picker,
    get_run_readiness,
    get_test_case_for_project,
    get_test_run_case_detail,
    get_test_run_for_project,
    get_test_run_summary,
    get_upload_for_project,
    is_valid_xml_filename,
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
    rerun_failed_cases,
    reset_test_run,
    save_application_context,
    save_project_prompt,
    start_test_run,
    start_upload_processing,
    suggest_run_name,
    unarchive_project,
    update_project,
    update_test_case,
    validate_testrail_xml,
)
from projects.tasks import refine_project_prompt_task
from projects.testrail_results_services import get_testrail_push_panel
from projects.testrail_services import get_testrail_picker_state

ALLOWED_PER_PAGE = [10, 20, 50, 100]
DEFAULT_PER_PAGE = 20
PROJECTS_DEFAULT_PER_PAGE = 9


def _parse_per_page(request: HttpRequest, default: int = DEFAULT_PER_PAGE) -> int:
    try:
        value = int(request.GET.get("per_page", ""))
    except ValueError:
        return default
    return value if value in ALLOWED_PER_PAGE else default


def _parse_page(request: HttpRequest) -> int:
    try:
        return max(1, int(request.GET.get("page", "1")))
    except ValueError:
        return 1


def _build_query_params(request: HttpRequest) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    encoded = params.urlencode()
    return f"&{encoded}" if encoded else ""


def _get_elided_page_range(page_obj: Page[Any]) -> list[int | str]:
    return [
        int(p) if isinstance(p, int) else str(p)
        for p in page_obj.paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1
        )
    ]


def _parse_test_case_ids(request: HttpRequest) -> list[int]:
    raw_ids = request.POST.getlist("test_case_ids")
    return [int(x) for x in raw_ids if x.isdigit()]


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
    page = _parse_page(request)
    per_page = _parse_per_page(request, PROJECTS_DEFAULT_PER_PAGE)

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

    return render(
        request,
        "projects/list.html",
        {
            "projects": projects,
            "latest_runs": latest_runs,
            "tags": tags,
            "form": form,
            "search": search or "",
            "current_tag": tag_filter or "",
            "show_archived": show_archived,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": _get_elided_page_range(projects),
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
    user = request.user
    project = get_project_for_user(project_id, user)
    if project is None:
        raise Http404

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
    user = request.user
    project = get_project_for_user(project_id, user)
    if project is None:
        raise Http404

    archive_project(project)
    return redirect("projects:list")


@login_required
@require_POST
def project_restore(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    """Restore an archived project. Uses `get_project_by_id` since the normal
    project lookup excludes archived projects."""
    project = get_project_by_id(project_id, request.user)
    if project is None:
        raise Http404

    unarchive_project(project)
    messages.success(request, f'"{project.name}" restored.')
    if request.POST.get("archived") == "1":
        return redirect(f"{reverse('projects:list')}?archived=1")
    return redirect("projects:list")


@login_required
@require_POST
def project_duplicate(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    user: CustomUser = request.user
    project = get_project_for_user(project_id, user)
    if project is None:
        raise Http404

    name = request.POST.get("name", "").strip() or f"Copy of {project.name}"
    duplicate_project(source_project=project, user=user, name=name)
    messages.success(request, f'Project duplicated as "{name}".')
    return redirect("projects:list")


# ============================================================================
# PROJECT OVERVIEW / ENVIRONMENT / APPLICATION CONTEXT VIEWS
# ============================================================================


@project_membership_required
def project_overview(request: HttpRequest, project: Project) -> HttpResponse:
    overview = get_project_overview(project)
    return render(
        request,
        "projects/overview.html",
        {
            "project": project,
            "overview": overview,
            "active_tab": "overview",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_environment(request: HttpRequest, project: Project) -> HttpResponse:
    machine_state = get_machine_state(project)
    return render(
        request,
        "projects/environment.html",
        {
            "project": project,
            "machine_state": machine_state,
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
    project: Project, form: ApplicationContextForm
) -> None:
    save_application_context(
        project=project,
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
            "active_tab": "overview",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_application_context(request: HttpRequest, project: Project) -> HttpResponse:
    """Standalone application-context edit page, linked from the overview."""
    if request.method == "GET":
        form = ApplicationContextForm(initial=_application_context_initial(project))
        return _render_application_context_page(request, project, form)

    form = ApplicationContextForm(request.POST)
    if not form.is_valid():
        return _render_application_context_page(request, project, form)

    _save_application_context_from_form(project, form)
    messages.success(request, "Application context saved.")
    return redirect("projects:detail", project_id=project.id)


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
def setup_context(request: HttpRequest, project: Project) -> HttpResponse:
    """Wizard step 2: application context."""
    if request.method == "GET":
        form = ApplicationContextForm(initial=_application_context_initial(project))
        return _render_wizard_context(request, project, form)

    form = ApplicationContextForm(request.POST)
    if not form.is_valid():
        return _render_wizard_context(request, project, form)

    _save_application_context_from_form(project, form)
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
    page = _parse_page(request)
    per_page = _parse_per_page(request)

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
            "elided_page_range": _get_elided_page_range(test_cases),
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
    target_project = get_project_for_user(int(target_project_id), user)
    if target_project is None:
        raise Http404

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


def _handle_xml_upload(request: AuthenticatedRequest, project: Project) -> str | None:
    """Validate and process an uploaded TestRail XML file.

    Returns None on success, or a human-readable error message on failure.
    """
    file = request.FILES.get("file")
    if not file:
        return "No file selected."
    if not is_valid_xml_filename(file.name or ""):
        return "Only .xml files are accepted."

    content = file.read().decode("utf-8", errors="replace")
    is_valid, error = validate_testrail_xml(content)
    if not is_valid:
        return error

    file.seek(0)
    upload = create_upload(project=project, user=request.user, file=file)
    start_upload_processing(upload)
    return None


@project_membership_required
def test_case_import(request: HttpRequest, project: Project) -> HttpResponse:
    """Display paginated upload history with the "Import from TestRail" dropzone."""
    page = _parse_page(request)
    per_page = _parse_per_page(request)
    uploads = list_uploads_for_project(project=project, page=page, per_page=per_page)
    upload_create_url = reverse("projects:upload_create", args=[project.id])
    return render(
        request,
        "projects/import.html",
        {
            "project": project,
            "uploads": uploads,
            "upload_create_url": upload_create_url,
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "elided_page_range": _get_elided_page_range(uploads),
            "query_params": _build_query_params(request),
            "testrail_picker": get_testrail_picker_state(project),
            "active_tab": "test_cases",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def upload_create(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    error = _handle_xml_upload(request, project)
    if error is not None:
        messages.error(request, error)
    default_url = reverse("projects:test_case_import", args=[project.id])
    return redirect(_next_or_default(request, default_url))


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
    page = _parse_page(request)
    per_page = _parse_per_page(request)
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
            "elided_page_range": _get_elided_page_range(test_runs),
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
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        start_test_run(test_run)
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
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        new_run = rerun_failed_cases(test_run)
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
    page = _parse_page(request)
    per_page = _parse_per_page(request)
    cases = list_test_run_cases(test_run=test_run, page=page, per_page=per_page)
    summary = get_test_run_summary(test_run)
    failed_titles = list_failed_case_titles(test_run)
    narrative = build_run_narrative(test_run, summary, failed_titles)
    can_rerun_failed = can_rerun_failed_cases(test_run, summary["failed"])

    context: dict[str, Any] = {
        "project": project,
        "test_run": test_run,
        "cases": cases,
        "summary": summary,
        "per_page": per_page,
        "allowed_per_page": ALLOWED_PER_PAGE,
        "elided_page_range": _get_elided_page_range(cases),
        "query_params": _build_query_params(request),
        "form": TestCaseForm(),
        "can_edit": can_edit_test_case_in_run(test_run),
        "narrative": narrative,
        "can_rerun_failed": can_rerun_failed,
        "failed_titles": failed_titles,
        "testrail_push": get_testrail_push_panel(
            test_run, links_base_url=request.build_absolute_uri("/")
        ),
        "active_tab": "runs",
        "active_nav": "projects",
    }
    if test_run.status == TestRunStatus.WAITING:
        context["readiness"] = get_run_readiness(project, test_run=test_run)
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
    screenshots = list(pivot.screenshots.all())
    form = TestCaseForm()
    can_edit = can_edit_test_case_in_run(pivot.test_run)
    case_position, case_total = get_case_position(pivot)
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
            "active_tab": "runs",
            "active_nav": "projects",
        },
    )


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
# ONBOARDING WIZARD — STEPS 3-5
# ============================================================================


@project_membership_required
def setup_cases(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Wizard step 3: import test cases."""
    if request.method == "POST":
        error = _handle_xml_upload(request, project)
        if error is not None:
            messages.error(request, error)
        return redirect("projects:setup_cases", project_id=project.id)

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


def _handle_setup_run_submit(request: HttpRequest, project: Project) -> HttpResponse:
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
        start_test_run(test_run)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
def setup_run(request: HttpRequest, project: Project) -> HttpResponse:
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
def project_save_prompt(request: HttpRequest, project: Project) -> HttpResponse:
    prompt = request.POST.get("project_prompt", "")[:PROJECT_PROMPT_MAX_LENGTH]
    save_project_prompt(project=project, prompt=prompt)
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
