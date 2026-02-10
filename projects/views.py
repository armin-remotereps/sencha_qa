from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.models import CustomUser
from projects.decorators import project_membership_required
from projects.forms import ProjectForm, TestCaseForm
from projects.models import Project
from projects.services import (
    archive_project,
    create_project,
    create_test_case,
    delete_test_case,
    get_all_tags_for_user,
    get_project_for_user,
    get_test_case_for_project,
    list_projects_for_user,
    list_test_cases_for_project,
    update_project,
    update_test_case,
)

PROJECTS_PER_PAGE = 9
TEST_CASES_PER_PAGE = 20


@login_required
def project_list(request: HttpRequest) -> HttpResponse:
    user: CustomUser = request.user  # type: ignore[assignment]
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
def project_create(request: HttpRequest) -> HttpResponse:
    user: CustomUser = request.user  # type: ignore[assignment]
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
def project_edit(request: HttpRequest, project_id: int) -> HttpResponse:
    user: CustomUser = request.user  # type: ignore[assignment]
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
def project_archive(request: HttpRequest, project_id: int) -> HttpResponse:
    user: CustomUser = request.user  # type: ignore[assignment]
    project = get_project_for_user(project_id, user)
    if project is None:
        raise Http404

    archive_project(project)
    return redirect("projects:list")


# ============================================================================
# TEST CASE VIEWS
# ============================================================================


@project_membership_required
def test_case_list(request: HttpRequest, project: Project) -> HttpResponse:
    search = request.GET.get("search", "").strip() or None
    page = request.GET.get("page", "1")

    test_cases = list_test_cases_for_project(
        project=project,
        search=search,
        page=int(page),
        per_page=TEST_CASES_PER_PAGE,
    )
    form = TestCaseForm()

    return render(
        request,
        "projects/test_cases.html",
        {
            "project": project,
            "test_cases": test_cases,
            "form": form,
            "search": search or "",
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
