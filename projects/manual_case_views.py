"""Manual run: release a stuck claim, pause and continue from the web.

Doing a manual case is runner-only (D5); a writable member may only hand a
claimed case back (Not started) when the tester's Test Runner is gone, or
pause / continue the claimed case (its clock stops while paused). Thin:
scope the case, call the service (``manual_case_claim_services`` /
``manual_case_pause_services``) and map its refusal to a flash message.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.case_help_services import get_run_case_for_help
from projects.decorators import project_membership_required
from projects.exceptions import ManualRunError
from projects.forms import ManualPauseForm, ManualReleaseForm
from projects.manual_case_claim_services import release_manual_case
from projects.manual_case_pause_services import (
    continue_manual_case,
    pause_manual_case,
)
from projects.models import Project, TestRunTestCase
from projects.person_step_actor import PersonStepActor


@project_membership_required
@require_POST
def test_run_case_manual_release(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    try:
        # None: a writable member's force release (no claim token).
        release_manual_case(
            project=project,
            pivot_id=pivot.id,
            actor=PersonStepActor.web(request.user),
            claim_token=None,
        )
    except ManualRunError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            "Case released. It is Not started again and can be picked at the "
            "Test Runner.",
        )
    form = ManualReleaseForm(request.POST)
    next_url = form.cleaned_data["next"] if form.is_valid() else ""
    return _redirect_back(request, project, pivot, next_url)


@project_membership_required
@require_POST
def test_run_case_manual_pause(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    try:
        # None: a writable member on the web (no claim token).
        paused = pause_manual_case(
            project=project,
            pivot_id=pivot.id,
            actor=PersonStepActor.web(request.user),
            claim_token=None,
        )
    except ManualRunError as exc:
        messages.error(request, exc.message)
    else:
        if paused:
            messages.success(
                request,
                "Case paused. Its clock is stopped until someone continues it.",
            )
        else:
            messages.info(request, "The case was already paused.")
    form = ManualPauseForm(request.POST)
    next_url = form.cleaned_data["next"] if form.is_valid() else ""
    return _redirect_back(request, project, pivot, next_url)


@project_membership_required
@require_POST
def test_run_case_manual_continue(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    try:
        # None: a writable member on the web (no claim token).
        continue_manual_case(
            project=project,
            pivot_id=pivot.id,
            actor=PersonStepActor.web(request.user),
            claim_token=None,
        )
    except ManualRunError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Testing continues. The case's clock runs again.")
    form = ManualPauseForm(request.POST)
    next_url = form.cleaned_data["next"] if form.is_valid() else ""
    return _redirect_back(request, project, pivot, next_url)


def _redirect_back(
    request: AuthenticatedRequest,
    project: Project,
    pivot: TestRunTestCase,
    next_url: str,
) -> HttpResponse:
    """To a same-host ``next``, else the case page."""
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(
        reverse(
            "projects:test_run_case_detail",
            kwargs={
                "project_id": project.id,
                "test_run_id": pivot.test_run_id,
                "pivot_id": pivot.id,
            },
        )
    )
