from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.exceptions import (
    AlreadyMemberError,
    LastProjectMemberError,
    MemberInviteThrottledError,
    MemberNotFoundError,
    NotProjectMemberError,
)
from projects.forms import ProjectMemberInviteForm
from projects.models import Project
from projects.project_member_services import invite_member, leave_project
from projects.testrail_views import render_project_settings


@project_membership_required
@require_POST
def member_invite(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Add an existing user to the project by email."""
    form = ProjectMemberInviteForm(request.POST)
    if form.is_valid():
        try:
            user = invite_member(
                project=project,
                inviter=request.user,
                email=form.cleaned_data["email"],
            )
        except (
            MemberInviteThrottledError,
            MemberNotFoundError,
            AlreadyMemberError,
        ) as exc:
            form.add_error("email", exc.message)
        else:
            messages.success(request, f"{user.email} now has access to this project.")
            return redirect("projects:settings", project_id=project.id)

    return render_project_settings(request, project, invite_form=form)


@project_membership_required
@require_POST
def project_leave(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Remove the current user from the project if another active member stays."""
    try:
        leave_project(project=project, user=request.user)
    except LastProjectMemberError as exc:
        messages.error(request, exc.message)
        return redirect("projects:settings", project_id=project.id)
    except NotProjectMemberError:
        return redirect("projects:list")
    messages.success(request, f"You left “{project.name}”.")
    return redirect("projects:list")
