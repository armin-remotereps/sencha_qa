from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import (
    project_membership_required,
    project_read_access_required,
)
from projects.exceptions import (
    AlreadyMemberError,
    InvalidMemberRoleError,
    LastProjectMemberError,
    LastWritableMemberError,
    MemberInviteThrottledError,
    MemberNotFoundError,
    MemberRoleChangeNotAllowedError,
    NotProjectMemberError,
)
from projects.forms import (
    ProjectMemberInviteForm,
    ProjectMemberRoleForm,
    form_error_text,
)
from projects.models import Project, ProjectRole
from projects.project_member_services import (
    change_member_role,
    invite_member,
    leave_project,
)
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
                role=(
                    ProjectRole.READ_ONLY
                    if form.cleaned_data["read_only"]
                    else ProjectRole.MEMBER
                ),
            )
        except (
            InvalidMemberRoleError,
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
def member_role(
    request: AuthenticatedRequest, project: Project, user_id: int
) -> HttpResponse:
    """Change one member's role (Member / Read-only) from the Members card."""
    settings_page = redirect("projects:settings", project_id=project.id)
    form = ProjectMemberRoleForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return settings_page
    role: str = form.cleaned_data["role"]
    try:
        change_member_role(
            project=project, actor=request.user, user_id=user_id, role=role
        )
    except NotProjectMemberError:
        messages.error(request, "That person is no longer a member of this project.")
    except (
        InvalidMemberRoleError,
        LastWritableMemberError,
        MemberRoleChangeNotAllowedError,
    ) as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Role changed to {ProjectRole(role).label}.")
    return settings_page


# Read access only: a read-only member may leave too.
@project_read_access_required
@require_POST
def project_leave(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    """Remove the current user from the project if the project keeps its members."""
    try:
        leave_project(project=project, user=request.user)
    except (LastProjectMemberError, LastWritableMemberError) as exc:
        messages.error(request, exc.message)
        return redirect("projects:settings", project_id=project.id)
    except NotProjectMemberError:
        return redirect("projects:list")
    messages.success(request, f"You left “{project.name}”.")
    return redirect("projects:list")
