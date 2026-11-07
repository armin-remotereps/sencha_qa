from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.exceptions import CaptureAppsError
from projects.forms import ProjectCaptureAppsForm
from projects.models import Project
from projects.project_capture_apps_services import set_capture_allowed_processes
from projects.testrail_views import render_project_settings


@project_membership_required
@require_POST
def settings_capture_apps(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Settings "Screenshot capture" card: save the allowed applications."""
    form = ProjectCaptureAppsForm(request.POST)
    if not form.is_valid():
        return render_project_settings(request, project, capture_apps_form=form)
    try:
        set_capture_allowed_processes(project=project, names=form.cleaned_data["apps"])
    except CaptureAppsError as exc:
        form.add_error("apps", str(exc))
        return render_project_settings(request, project, capture_apps_form=form)
    messages.success(request, "Allowed applications saved.")
    settings_url = reverse("projects:settings", kwargs={"project_id": project.id})
    return redirect(f"{settings_url}#screenshot-capture")
