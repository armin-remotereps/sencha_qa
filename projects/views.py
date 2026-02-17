from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.forms import ProjectForm, TestCaseForm
from projects.models import Project, TestCaseUpload, UploadStatus
from projects.services import (
    add_cases_to_test_run,
    archive_project,
    cancel_upload_processing,
    create_project,
    create_test_case,
    create_test_run_with_cases,
    create_upload,
    delete_test_case,
    delete_test_run,
    delete_upload,
    generate_controller_client_zip,
    get_all_tags_for_user,
    get_project_for_user,
    get_test_case_for_project,
    get_test_run_case_detail,
    get_test_run_for_project,
    get_test_run_summary,
    get_upload_for_project,
    is_valid_xml_filename,
    list_projects_for_user,
    list_test_cases_for_project,
    list_test_run_cases,
    list_test_runs_for_project,
    list_uploads_for_project,
    list_waiting_test_runs_for_project,
    redo_test_run,
    regenerate_api_key,
    remove_case_from_test_run,
    start_test_run,
    start_upload_processing,
    update_project,
    update_test_case,
    validate_testrail_xml,
)

PROJECTS_PER_PAGE = 9
TEST_CASES_PER_PAGE = 20
UPLOADS_PER_PAGE = 20
TEST_RUNS_PER_PAGE = 20
TEST_RUN_CASES_PER_PAGE = 20


def _parse_test_case_ids(request: HttpRequest) -> list[int]:
    raw_ids = request.POST.getlist("test_case_ids")
    return [int(x) for x in raw_ids if x.isdigit()]


@login_required
def project_list(request: AuthenticatedRequest) -> HttpResponse:
    user = request.user
    search = request.GET.get("search", "").strip() or None
    tag_filter = request.GET.get("tag", "").strip() or None
    page = request.GET.get("page", "1")

    projects = list_projects_for_user(
        user=user,
        search=search,
        tag_filter=tag_filter,
        page=int(page),
        per_page=PROJECTS_PER_PAGE,
    )
    tags = get_all_tags_for_user(user)
    form = ProjectForm()

    return render(
        request,
        "projects/list.html",
        {
            "projects": projects,
            "tags": tags,
            "form": form,
            "search": search or "",
            "current_tag": tag_filter or "",
        },
    )


@login_required
@require_POST
def project_create(request: AuthenticatedRequest) -> HttpResponse:
    user = request.user
    form = ProjectForm(request.POST)
    if form.is_valid():
        create_project(
            user=user,
            name=form.cleaned_data["name"],
            tag_names=form.cleaned_data["tags"],
        )
    return redirect("projects:list")


@login_required
@require_POST
def project_edit(request: AuthenticatedRequest, project_id: int) -> HttpResponse:
    user = request.user
    project = get_project_for_user(project_id, user)
    if project is None:
        raise Http404

    form = ProjectForm(request.POST)
    if form.is_valid():
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


@project_membership_required
def project_detail(request: HttpRequest, project: Project) -> HttpResponse:
    return render(request, "projects/detail.html", {"project": project})


@project_membership_required
@require_POST
def project_regenerate_api_key(request: HttpRequest, project: Project) -> HttpResponse:
    regenerate_api_key(project)
    messages.success(request, "API key regenerated successfully.")
    return redirect("projects:detail", project_id=project.id)


@project_membership_required
@require_POST
def download_controller_client(request: HttpRequest, project: Project) -> HttpResponse:
    """Generate and download the controller client ZIP for the project."""
    zip_bytes = generate_controller_client_zip(project)
    response = HttpResponse(zip_bytes, content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="controller-client-{project.id}.zip"'
    )
    return response


# ============================================================================
# TEST CASE VIEWS
# ============================================================================


@project_membership_required
def test_case_list(request: HttpRequest, project: Project) -> HttpResponse:
    """Display paginated test cases with optional search and upload filter."""
    search = request.GET.get("search", "").strip() or None
    upload_filter = request.GET.get("upload", "").strip() or None
    upload_id: int | None = int(upload_filter) if upload_filter else None
    page = request.GET.get("page", "1")

    test_cases = list_test_cases_for_project(
        project=project,
        search=search,
        upload_id=upload_id,
        page=int(page),
        per_page=TEST_CASES_PER_PAGE,
    )
    form = TestCaseForm()
    completed_uploads = TestCaseUpload.objects.filter(
        project=project, status=UploadStatus.COMPLETED
    ).order_by("-created_at")
    waiting_test_runs = list_waiting_test_runs_for_project(project)

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
            "waiting_test_runs": waiting_test_runs,
        },
    )


@project_membership_required
@require_POST
def test_case_create(request: HttpRequest, project: Project) -> HttpResponse:
    form = TestCaseForm(request.POST)
    if form.is_valid():
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
    if form.is_valid():
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


# ============================================================================
# UPLOAD VIEWS
# ============================================================================


@project_membership_required
def upload_list(request: HttpRequest, project: Project) -> HttpResponse:
    """Display paginated upload history with drag-drop upload zone."""
    page = request.GET.get("page", "1")
    uploads = list_uploads_for_project(
        project=project, page=int(page), per_page=UPLOADS_PER_PAGE
    )
    upload_create_url = reverse("projects:upload_create", args=[project.id])
    return render(
        request,
        "projects/uploads.html",
        {
            "project": project,
            "uploads": uploads,
            "upload_create_url": upload_create_url,
        },
    )


@project_membership_required
@require_POST
def upload_create(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Validate and process an uploaded TestRail XML file."""
    file = request.FILES.get("file")
    if not file:
        return redirect("projects:upload_list", project_id=project.id)

    if not is_valid_xml_filename(file.name or ""):
        return redirect("projects:upload_list", project_id=project.id)

    content = file.read().decode("utf-8", errors="replace")
    is_valid, _error = validate_testrail_xml(content)
    if not is_valid:
        return redirect("projects:upload_list", project_id=project.id)

    file.seek(0)
    user = request.user
    upload = create_upload(project=project, user=user, file=file)
    start_upload_processing(upload)
    return redirect("projects:upload_list", project_id=project.id)


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
    return redirect("projects:upload_list", project_id=project.id)


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
    return redirect("projects:upload_list", project_id=project.id)


# ============================================================================
# TEST RUN VIEWS
# ============================================================================


@project_membership_required
def test_run_list(request: HttpRequest, project: Project) -> HttpResponse:
    page = request.GET.get("page", "1")
    test_runs = list_test_runs_for_project(
        project=project, page=int(page), per_page=TEST_RUNS_PER_PAGE
    )
    return render(
        request,
        "projects/test_runs.html",
        {
            "project": project,
            "test_runs": test_runs,
        },
    )


@project_membership_required
@require_POST
def test_run_create(request: HttpRequest, project: Project) -> HttpResponse:
    test_case_ids = _parse_test_case_ids(request)
    if not test_case_ids:
        return redirect("projects:test_case_list", project_id=project.id)
    test_run = create_test_run_with_cases(project=project, test_case_ids=test_case_ids)
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
def test_run_redo(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    try:
        redo_test_run(test_run)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(
        "projects:test_run_detail", project_id=project.id, test_run_id=test_run.id
    )


@project_membership_required
def test_run_detail(
    request: HttpRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    page = request.GET.get("page", "1")
    cases = list_test_run_cases(
        test_run=test_run, page=int(page), per_page=TEST_RUN_CASES_PER_PAGE
    )
    summary = get_test_run_summary(test_run)
    return render(
        request,
        "projects/test_run_detail.html",
        {
            "project": project,
            "test_run": test_run,
            "cases": cases,
            "summary": summary,
        },
    )


@project_membership_required
def test_run_case_detail(
    request: HttpRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_test_run_case_detail(pivot_id, project)
    if pivot is None or pivot.test_run_id != test_run_id:
        raise Http404
    screenshots = pivot.screenshots.all()
    return render(
        request,
        "projects/test_run_case_detail.html",
        {
            "project": project,
            "test_run": pivot.test_run,
            "pivot": pivot,
            "screenshots": screenshots,
        },
    )
