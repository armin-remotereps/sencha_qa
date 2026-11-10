from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.capture_phone_apps_services import load_phone_apps
from projects.decorators import project_membership_required
from projects.exceptions import CaptureAppsError, CapturePhoneAppsError
from projects.forms import (
    CapturePhoneAppsLoadForm,
    ProjectCaptureAppsAddForm,
    ProjectCaptureAppsForm,
    form_error_text,
)
from projects.models import Project
from projects.project_capture_apps_services import (
    add_capture_allowed_processes,
    set_capture_allowed_processes,
)
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


@project_membership_required
@require_POST
def settings_capture_phone_apps(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Settings "Screenshot capture" card: load the phone's installed apps
    into the picker from the phone runner the person picked (a synchronous,
    bounded round trip to that Test Runner)."""
    form = CapturePhoneAppsLoadForm(request.POST)
    if not form.is_valid():
        return render_project_settings(
            request, project, phone_apps_error=form_error_text(form)
        )
    try:
        phone_apps = load_phone_apps(
            project, form.cleaned_data["runner"], actor=request.user
        )
    except CapturePhoneAppsError as exc:
        return render_project_settings(request, project, phone_apps_error=str(exc))
    return render_project_settings(request, project, phone_apps=phone_apps)


@project_membership_required
@require_POST
def settings_capture_apps_add(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Settings "Screenshot capture" card: add the apps ticked in the picker."""
    form = ProjectCaptureAppsAddForm(request.POST)
    if not form.is_valid():
        errors = form.errors.get("apps") or ["The selection could not be saved."]
        return render_project_settings(
            request, project, capture_apps_add_error=str(errors[0])
        )
    try:
        count = add_capture_allowed_processes(
            project=project, app_ids=form.cleaned_data["apps"]
        )
    except CaptureAppsError as exc:
        return render_project_settings(
            request, project, capture_apps_add_error=str(exc)
        )
    messages.success(request, "Added 1 app." if count == 1 else f"Added {count} apps.")
    settings_url = reverse("projects:settings", kwargs={"project_id": project.id})
    return redirect(f"{settings_url}#screenshot-capture")
