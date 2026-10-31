"""Machine readiness actions that call the Test Runner.

They live in ``projects`` because they need the runner ports, which only
``projects`` composes (``projects.machine_readiness_ports``; plan D2). The
rest of the Machine readiness tab is ``machine_readiness.views``.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from machine_readiness.exceptions import ArtifactScanUnavailableError
from machine_readiness.services.request_scan import request_scan
from projects.decorators import project_membership_required
from projects.machine_readiness_ports import build_runner_ports
from projects.models import Project


@project_membership_required
@require_POST
def machine_readiness_scan(
    request: AuthenticatedRequest, project: Project
) -> HttpResponse:
    """Queue a scan of the runner OS's base directory."""
    try:
        availability = request_scan(
            project_id=project.id,
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
            "not change, the Machine setup chat says why the scan did not run.",
        )
    return redirect("machine_readiness:overview", project_id=project.id)
