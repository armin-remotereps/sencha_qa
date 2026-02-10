from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.models import CustomUser
from projects.forms import ProjectForm
from projects.services import (
    archive_project,
    create_project,
    get_all_tags_for_user,
    get_project_for_user,
    list_projects_for_user,
    update_project,
)

PROJECTS_PER_PAGE = 9


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
