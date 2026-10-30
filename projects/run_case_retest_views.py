"""Mark run cases Needs re-test (run page bulk bar, case page) and undo it.

Thin views: validate the form, call ``run_case_retest_services`` and map its
refusals to flash messages.
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
from projects.exceptions import RunCaseRetestError
from projects.forms import RunCasesRetestForm, form_error_text
from projects.models import Project
from projects.run_case_retest_services import (
    DRAFT_RUN_MESSAGE,
    format_retest_message,
    is_retest_open,
    list_retestable_pivot_ids,
    mark_cases_for_retest,
    undo_retest,
)
from projects.services import get_test_run_for_project


@project_membership_required
@require_POST
def test_run_cases_retest(
    request: AuthenticatedRequest, project: Project, test_run_id: int
) -> HttpResponse:
    test_run = get_test_run_for_project(test_run_id, project)
    if test_run is None:
        raise Http404
    form = RunCasesRetestForm(list_retestable_pivot_ids(test_run), request.POST)
    if not is_retest_open(test_run):
        messages.error(request, DRAFT_RUN_MESSAGE)
    elif not form.is_valid():
        messages.error(request, form_error_text(form))
    else:
        try:
            outcome = mark_cases_for_retest(
                test_run=test_run,
                pivot_ids=form.cleaned_data["pivot_ids"],
                user=request.user,
                note=form.cleaned_data["note"],
            )
        except RunCaseRetestError as exc:
            messages.error(request, exc.message)
        else:
            if outcome.marked:
                messages.success(request, format_retest_message(outcome))
            if outcome.problem:
                messages.error(request, outcome.problem)
    fallback_url = reverse(
        "projects:test_run_detail",
        kwargs={"project_id": project.id, "test_run_id": test_run.id},
    )
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect(fallback_url)


@project_membership_required
@require_POST
def test_run_case_retest_undo(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    pivot = get_run_case_for_help(pivot_id, test_run_id, project)
    if pivot is None:
        raise Http404
    try:
        undo_retest(pivot=pivot, user=request.user)
    except RunCaseRetestError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Re-test mark undone; the earlier result is back.")
    return redirect(
        "projects:test_run_case_detail",
        project_id=project.id,
        test_run_id=pivot.test_run_id,
        pivot_id=pivot.id,
    )
