"""A person's actions on a step handed to them (Human in the loop).

Each view validates its form, calls one ``person_step_services`` use case,
maps ``PersonStepError`` to a flash message and returns to the case page.
"""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from accounts.types import AuthenticatedRequest
from projects.case_help_services import get_run_case_for_help
from projects.decorators import project_membership_required
from projects.exceptions import PersonStepError
from projects.forms import (
    PersonStepDecisionForm,
    PersonStepReportForm,
    form_error_text,
)
from projects.models import Project, TestRunTestCase
from projects.person_step_actor import PersonStepActor
from projects.person_step_services import (
    PersonStepActionResult,
    mark_blocked_person_step,
    mark_failed_person_step,
    overrule_person_step,
    report_person_step,
)


@project_membership_required
@require_POST
def test_run_case_person_report(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _case_or_404(project, test_run_id, pivot_id)
    form = PersonStepReportForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_redirect(project, pivot)
    try:
        result = report_person_step(
            pivot=pivot,
            actor=PersonStepActor.web(request.user),
            person_step_id=form.cleaned_data["person_step_id"],
            outcome=form.cleaned_data["outcome"],
            note=form.cleaned_data["note"],
            output=form.cleaned_data["output"],
        )
    except PersonStepError as exc:
        messages.error(request, exc.message)
    else:
        _flash_result(request, "Reported. The agent is checking it.", result)
    return _case_redirect(project, pivot)


@project_membership_required
@require_POST
def test_run_case_person_overrule(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _case_or_404(project, test_run_id, pivot_id)
    form = PersonStepDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_redirect(project, pivot)
    try:
        result = overrule_person_step(
            pivot=pivot,
            actor=PersonStepActor.web(request.user),
            person_step_id=form.cleaned_data["person_step_id"],
            note=form.cleaned_data["note"],
        )
    except PersonStepError as exc:
        messages.error(request, exc.message)
    else:
        _flash_result(
            request, "Overruled. The step is recorded as passed by you.", result
        )
    return _case_redirect(project, pivot)


@project_membership_required
@require_POST
def test_run_case_person_fail(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _case_or_404(project, test_run_id, pivot_id)
    form = PersonStepDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_redirect(project, pivot)
    try:
        result = mark_failed_person_step(
            pivot=pivot,
            actor=PersonStepActor.web(request.user),
            person_step_id=form.cleaned_data["person_step_id"],
            note=form.cleaned_data["note"],
        )
    except PersonStepError as exc:
        messages.error(request, exc.message)
    else:
        _flash_result(request, "Marked the case failed. The run continues.", result)
    return _case_redirect(project, pivot)


@project_membership_required
@require_POST
def test_run_case_person_block(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = _case_or_404(project, test_run_id, pivot_id)
    form = PersonStepDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _case_redirect(project, pivot)
    try:
        result = mark_blocked_person_step(
            pivot=pivot,
            actor=PersonStepActor.web(request.user),
            person_step_id=form.cleaned_data["person_step_id"],
            note=form.cleaned_data["note"],
        )
    except PersonStepError as exc:
        messages.error(request, exc.message)
    else:
        _flash_result(request, "Marked the case blocked. The run continues.", result)
    return _case_redirect(project, pivot)


def _case_or_404(project: Project, test_run_id: int, pivot_id: int) -> TestRunTestCase:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    return pivot


def _case_redirect(project: Project, pivot: TestRunTestCase) -> HttpResponse:
    return redirect(
        "projects:test_run_case_detail",
        project_id=project.id,
        test_run_id=pivot.test_run_id,
        pivot_id=pivot.id,
    )


def _flash_result(
    request: AuthenticatedRequest, success: str, result: PersonStepActionResult
) -> None:
    if result.queued:
        messages.success(request, success)
        return
    messages.warning(
        request,
        f"Saved, but the run cannot continue yet: {result.blocker} "
        "Then use Continue run on the run page.",
    )
