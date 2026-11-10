"""Machine readiness actions that call the Test Runner.

They live in ``projects`` because they need the runner ports, which only
``projects`` composes (``projects.machine_readiness_ports``; plan D2). The
rest of the Machine readiness tab is ``machine_readiness.views``.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from machine_readiness.exceptions import ArtifactScanUnavailableError
from machine_readiness.services.request_scan import request_scan
from projects.decorators import project_membership_required
from projects.forms import RunRunnerForm, form_error_text
from projects.machine_readiness_ports import build_runner_ports
from projects.models import Project
from projects.runner_choices import runner_choices


@project_membership_required
@require_POST
def machine_readiness_scan(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Queue a scan of the base directory on the Test Runner picked on the
    tab (one of the viewer's own runners, posted as ``runner``)."""
    form = RunRunnerForm(runner_choices(request.user, project), request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect("machine_readiness:overview", project_id=project.id)
    try:
        availability = request_scan(
            project_id=project.id,
            runner_id=form.cleaned_data["runner"],
            actor_id=request.user.id,
            ports=build_runner_ports(),
        )
    except ArtifactScanUnavailableError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            f"Scanning the {availability.runner_os_label} base directory. "
            "Reload this page in a few seconds to see the inventory; if it does "
            "not change, an open Machine setup chat shows why the scan did not run.",
        )
    # Keep the tab on the runner that was scanned.
    overview = reverse("machine_readiness:overview", kwargs={"project_id": project.id})
    return HttpResponseRedirect(f"{overview}?runner={form.cleaned_data['runner']}")
