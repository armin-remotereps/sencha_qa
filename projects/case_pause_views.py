"""Pause a running case to discuss it, and dismiss a "Too late to pause" notice.

Thin views: validate the form, call ``case_pause_services`` and map its
outcome or refusal to a flash message.
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
from projects.case_pause_policy import PAUSE_SECOND_MESSAGE, CasePauseOutcome
from projects.case_pause_services import dismiss_dropped_pause, request_case_pause
from projects.decorators import project_membership_required
from projects.exceptions import CasePauseError
from projects.forms import CasePauseForm, form_error_text
from projects.models import Project, TestRunTestCase
from projects.person_step_actor import PersonStepActor

_REQUESTED_MESSAGE = (
    "Pausing: the agent stops at its next action; the discussion opens when "
    "it has stopped."
)
_PAUSED_MESSAGE = "Paused; opening the discussion."


def _pivot_or_404(project: Project, test_run_id: int, pivot_id: int) -> TestRunTestCase:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    return pivot


def _case_url(project: Project, pivot: TestRunTestCase) -> str:
    return reverse(
        "projects:test_run_case_detail",
        kwargs={
            "project_id": project.id,
            "test_run_id": pivot.test_run_id,
            "pivot_id": pivot.id,
        },
    )


def _safe_next(request: AuthenticatedRequest, next_url: str, default_url: str) -> str:
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return default_url


@project_membership_required
@require_POST
def test_run_case_pause(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _pivot_or_404(project, test_run_id, pivot_id)
    case_url = _case_url(project, pivot)
    form = CasePauseForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return redirect(case_url)
    message: str = form.cleaned_data["message"]
    try:
        outcome = request_case_pause(
            pivot=pivot, actor=PersonStepActor.web(request.user), message=message
        )
    except CasePauseError as exc:
        messages.error(request, exc.message)
    else:
        if outcome == CasePauseOutcome.REQUESTED:
            messages.success(request, _REQUESTED_MESSAGE)
        elif outcome == CasePauseOutcome.ALREADY_REQUESTED:
            # The lookup defers this field, so reading it loads the first
            # requester's label as stored now (one small query).
            label = pivot.pause_requested_label
            text = (
                f"Already pausing (requested by {label})."
                if label
                else "Already pausing."
            )
            if message:
                text = f"{text} {PAUSE_SECOND_MESSAGE}"
            messages.info(request, text)
        else:
            messages.success(request, _PAUSED_MESSAGE)
    return redirect(_safe_next(request, form.cleaned_data["next"], case_url))


@project_membership_required
@require_POST
def test_run_case_pause_dismiss(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _pivot_or_404(project, test_run_id, pivot_id)
    try:
        dismiss_dropped_pause(pivot=pivot, user=request.user)
    except CasePauseError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Pause notice dismissed.")
    return redirect(_case_url(project, pivot))
