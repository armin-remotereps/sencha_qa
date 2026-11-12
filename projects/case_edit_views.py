"""Admin edit of a finished run case's result: editor page and its actions.

An admin (an active superuser) corrects a Passed, Failed or Blocked case: its
status, summary and plan steps, the screenshots' TestRail attach setting,
deletes or uploads screenshots and reverts the newest edit. Thin: scope the
case (``case_edit_page.get_case_for_edit``, ``CaseEditNotFoundError`` to 404),
validate the form (``case_edit_forms``), call the service and map its
refusals — ``CaseEditForbiddenError`` to 403, ``CaseEditError`` to a flash and
a redirect to the editor. The services enforce the admin check again under their locks.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.forms import BaseForm, Form
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from accounts.types import AuthenticatedRequest
from projects.case_edit_checkpoint import StepRow, step_rows
from projects.case_edit_forms import (
    CaseEditRevertForm,
    CaseResultEditForm,
    CaseStepEditFormSet,
    ScreenshotDeleteForm,
    ScreenshotEvidenceForm,
    ScreenshotUploadForm,
    cleaned_step_edits,
    step_formset_initial,
)
from projects.case_edit_page import build_case_edit_page, get_case_for_edit
from projects.case_edit_policy import can_admin_edit
from projects.case_edit_result_services import edit_case_result
from projects.case_edit_revert_services import revert_case_edit
from projects.case_edit_screenshot_services import (
    delete_case_screenshot,
    set_screenshot_evidence,
)
from projects.case_edit_upload_services import upload_case_screenshot
from projects.decorators import project_membership_required
from projects.exceptions import (
    CaseEditError,
    CaseEditForbiddenError,
    CaseEditNotFoundError,
)
from projects.models import EvidenceOverride, Project, TestRun, TestRunTestCase

STEP_FORMSET_PREFIX = "steps"
SHOTS_ANCHOR = "shots"
_UPLOAD_TOO_LARGE = "This screenshot is larger than the server accepts."


@dataclass(frozen=True)
class StepCard:
    """One plan step on the editor: its current values and its form."""

    row: StepRow
    form: Form


def _editor_url(project: Project, test_run_id: int, pivot_id: int) -> str:
    return reverse(
        "projects:test_run_case_result_edit", args=[project.id, test_run_id, pivot_id]
    )


def _case_url(project: Project, test_run_id: int, pivot_id: int) -> str:
    return reverse(
        "projects:test_run_case_detail", args=[project.id, test_run_id, pivot_id]
    )


def _to_shots(project: Project, test_run_id: int, pivot_id: int) -> HttpResponse:
    return redirect(_editor_url(project, test_run_id, pivot_id) + f"#{SHOTS_ANCHOR}")


def _require_admin(request: AuthenticatedRequest) -> None:
    if not can_admin_edit(request.user):
        raise PermissionDenied


def _case_or_404(
    project: Project, test_run_id: int, pivot_id: int
) -> tuple[TestRun, TestRunTestCase]:
    try:
        return get_case_for_edit(
            project_id=project.id, run_id=test_run_id, pivot_id=pivot_id
        )
    except CaseEditNotFoundError as exc:
        raise Http404 from exc


def _flash_form_errors(request: AuthenticatedRequest, form: BaseForm) -> None:
    """Show an invalid action form's errors as one flash (escaped by the page)."""
    texts = [str(error) for errors in form.errors.values() for error in errors]
    messages.error(request, " ".join(texts) or "The form was not valid.")


@project_membership_required
@require_http_methods(["GET", "POST"])
def test_run_case_result_edit(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    _require_admin(request)
    test_run, pivot = _case_or_404(project, test_run_id, pivot_id)
    if request.method == "POST":
        form = CaseResultEditForm(request.POST)
        step_formset = CaseStepEditFormSet(request.POST, prefix=STEP_FORMSET_PREFIX)
        if form.is_valid() and step_formset.is_valid():
            try:
                edit_case_result(
                    pivot_id=pivot.id,
                    user=request.user,
                    base_key=form.cleaned_data["base_key"],
                    status=form.cleaned_data["status"],
                    result=form.cleaned_data["result"],
                    steps=cleaned_step_edits(step_formset),
                )
            except CaseEditForbiddenError as exc:
                raise PermissionDenied from exc
            except CaseEditError as exc:
                messages.error(request, exc.message)
                return redirect(_editor_url(project, test_run_id, pivot.id))
            messages.success(
                request,
                "Result saved. Check the case again before sending it to TestRail.",
            )
            return HttpResponseRedirect(_case_url(project, test_run_id, pivot.id))
        # Invalid: the page is built only now, to show the errors.
        page = build_case_edit_page(pivot=pivot, run=test_run)
    else:
        page = build_case_edit_page(pivot=pivot, run=test_run)
        form = CaseResultEditForm(
            initial={
                "status": page.status,
                "result": page.result,
                "base_key": page.base_key,
            }
        )
        step_formset = CaseStepEditFormSet(
            initial=step_formset_initial(page.step_rows), prefix=STEP_FORMSET_PREFIX
        )
    step_nums = [row.step_num for row in page.step_rows]
    return render(
        request,
        "projects/test_run_case_edit.html",
        {
            "project": project,
            "test_run": test_run,
            "pivot": pivot,
            "page": page,
            "form": form,
            "step_formset": step_formset,
            # The plan rows and their forms side by side (same order: the
            # formset's initial data is built from page.step_rows).
            "step_cards": [
                StepCard(row=row, form=step_form)
                for row, step_form in zip(page.step_rows, step_formset.forms)
            ],
            "upload_form": ScreenshotUploadForm(
                step_nums, initial={"base_key": page.base_key}
            ),
            "override_choices": EvidenceOverride.choices,
            "active_tab": "runs",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_POST
def test_run_case_result_edit_shot_evidence(
    request: AuthenticatedRequest,
    project: Project,
    test_run_id: int,
    pivot_id: int,
    shot_id: int,
) -> HttpResponse:
    _require_admin(request)
    _test_run, pivot = _case_or_404(project, test_run_id, pivot_id)
    form = ScreenshotEvidenceForm(request.POST)
    if not form.is_valid():
        _flash_form_errors(request, form)
        return _to_shots(project, test_run_id, pivot.id)
    try:
        set_screenshot_evidence(
            pivot_id=pivot.id,
            shot_id=shot_id,
            user=request.user,
            base_key=form.cleaned_data["base_key"],
            override=form.cleaned_data["override"],
        )
    except CaseEditForbiddenError as exc:
        raise PermissionDenied from exc
    except CaseEditError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Screenshot attach setting saved.")
    return _to_shots(project, test_run_id, pivot.id)


@project_membership_required
@require_POST
def test_run_case_result_edit_shot_delete(
    request: AuthenticatedRequest,
    project: Project,
    test_run_id: int,
    pivot_id: int,
    shot_id: int,
) -> HttpResponse:
    _require_admin(request)
    _test_run, pivot = _case_or_404(project, test_run_id, pivot_id)
    form = ScreenshotDeleteForm(request.POST)
    if not form.is_valid():
        _flash_form_errors(request, form)
        return _to_shots(project, test_run_id, pivot.id)
    try:
        delete_case_screenshot(
            pivot_id=pivot.id,
            shot_id=shot_id,
            user=request.user,
            base_key=form.cleaned_data["base_key"],
        )
    except CaseEditForbiddenError as exc:
        raise PermissionDenied from exc
    except CaseEditError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Screenshot deleted.")
    return _to_shots(project, test_run_id, pivot.id)


@project_membership_required
@require_POST
def test_run_case_result_edit_shot_upload(
    request: AuthenticatedRequest, project: Project, test_run_id: int, pivot_id: int
) -> HttpResponse:
    _require_admin(request)
    _test_run, pivot = _case_or_404(project, test_run_id, pivot_id)
    form = ScreenshotUploadForm(
        [row.step_num for row in step_rows(pivot.execution_checkpoint)],
        request.POST,
        request.FILES,
    )
    if not form.is_valid():
        _flash_form_errors(request, form)
        return _to_shots(project, test_run_id, pivot.id)
    uploaded_file = form.cleaned_data["image"]
    if uploaded_file.size is not None and (
        uploaded_file.size > settings.MANUAL_SHOT_MAX_BYTES
    ):
        messages.error(request, _UPLOAD_TOO_LARGE)
        return _to_shots(project, test_run_id, pivot.id)
    # One byte over the cap is enough for the service to refuse an oversized
    # file (size unknown above) without reading all of it into memory.
    data = uploaded_file.read(settings.MANUAL_SHOT_MAX_BYTES + 1)
    try:
        upload_case_screenshot(
            pivot_id=pivot.id,
            user=request.user,
            base_key=form.cleaned_data["base_key"],
            data=data,
            step_num=form.cleaned_data["step_num"],
        )
    except CaseEditForbiddenError as exc:
        raise PermissionDenied from exc
    except CaseEditError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Screenshot uploaded.")
    return _to_shots(project, test_run_id, pivot.id)


@project_membership_required
@require_POST
def test_run_case_result_edit_revert(
    request: AuthenticatedRequest,
    project: Project,
    test_run_id: int,
    pivot_id: int,
    edit_id: int,
) -> HttpResponse:
    _require_admin(request)
    _test_run, pivot = _case_or_404(project, test_run_id, pivot_id)
    form = CaseEditRevertForm(request.POST)
    if not form.is_valid():
        _flash_form_errors(request, form)
        return redirect(_editor_url(project, test_run_id, pivot.id))
    try:
        revert_case_edit(pivot_id=pivot.id, edit_id=edit_id, user=request.user)
    except CaseEditForbiddenError as exc:
        raise PermissionDenied from exc
    except CaseEditError as exc:
        messages.error(request, exc.message)
        return redirect(_editor_url(project, test_run_id, pivot.id))
    messages.success(
        request,
        "Edit reverted. Check the case again before sending it to TestRail.",
    )
    return HttpResponseRedirect(_case_url(project, test_run_id, pivot.id))
