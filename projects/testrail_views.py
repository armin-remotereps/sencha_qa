from __future__ import annotations

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from projects.decorators import project_membership_required
from projects.forms import TestRailSettingsForm
from projects.models import Project
from projects.testrail_services import (
    check_testrail_connection,
    clear_testrail_settings,
    get_testrail_api_key_hint,
    save_testrail_settings,
)


def _settings_initial(project: Project) -> dict[str, str]:
    return {
        "testrail_url": project.testrail_url,
        "testrail_email": project.testrail_email,
    }


def _render_settings_page(
    request: HttpRequest, project: Project, form: TestRailSettingsForm
) -> HttpResponse:
    return render(
        request,
        "projects/settings.html",
        {
            "project": project,
            "form": form,
            "api_key_hint": get_testrail_api_key_hint(project),
            "active_tab": "settings",
            "active_nav": "projects",
        },
    )


@project_membership_required
def project_settings(request: HttpRequest, project: Project) -> HttpResponse:
    """Settings tab: TestRail integration card."""
    has_stored_key = bool(project.testrail_api_key_encrypted)
    if request.method == "GET":
        form = TestRailSettingsForm(
            initial=_settings_initial(project), has_stored_key=has_stored_key
        )
        return _render_settings_page(request, project, form)

    form = TestRailSettingsForm(request.POST, has_stored_key=has_stored_key)
    if not form.is_valid():
        return _render_settings_page(request, project, form)

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
