"""Environment page: a member asks the connected Test Runner to update itself.

The view validates the form, calls ``request_runner_update`` once, maps
``RunnerUpdateRefusedError`` to a flash message and returns to the
Environment page, where the Connection card shows the live phases. No extra
throttle: the one-in-flight constraint on ``RunnerUpdate`` refuses a second
request while one is running.
"""

from __future__ import annotations

from accounts.types import AuthenticatedRequest
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from projects.decorators import project_membership_required
from projects.exceptions import RunnerUpdateRefusedError
from projects.forms import RunnerUpdateForm
from projects.models import Project
from projects.runner_update_request import request_runner_update

_STALE_FORM_MESSAGE = "Reload the page and try again."


@project_membership_required
@require_POST
def runner_update(request: AuthenticatedRequest, project: Project) -> HttpResponse:
    form = RunnerUpdateForm(request.POST)
    if not form.is_valid():
        messages.error(request, _STALE_FORM_MESSAGE)
        return redirect("projects:environment", project_id=project.id)
    try:
        update = request_runner_update(
            project=project,
            user=request.user,
            expected_version=form.cleaned_data["expected_version"],
        )
    except RunnerUpdateRefusedError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            f"Update to Test Runner {update.to_version} sent. "
            "Follow its progress on the Connection card.",
        )
    return redirect("projects:environment", project_id=project.id)
