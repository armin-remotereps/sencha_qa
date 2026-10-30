"""The Failures page (``/failures/``): list, by-case view, CSV export, bulk
actions and saved filter presets.

Thin views: bind the forms with choices from the services, call a service,
map domain errors to messages. Every query is scoped to the user's member,
non-archived projects (``member_project_choices``, or with the writable ids
``member_project_choices_and_writable_ids``), so a project or run id
from another project is an invalid choice, and the bulk service re-resolves
every selected id itself, against the writable projects only.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Page
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseRedirect,
    QueryDict,
    StreamingHttpResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_safe

from accounts.types import AuthenticatedRequest
from auto_tester.pagination import (
    ALLOWED_PER_PAGE,
    elided_page_range,
    parse_page,
    parse_per_page,
)
from dashboard.exceptions import (
    ExportThrottledError,
    PresetLimitError,
    PresetNotFoundError,
)
from dashboard.forms import (
    FailedCaseBulkForm,
    FailedCaseFilterForm,
    FailedCasePresetForm,
)
from dashboard.services.export_throttle import check_export_throttle
from dashboard.services.failed_case_bulk import (
    apply_failed_case_bulk,
    failed_case_no_source_ids,
    failed_case_sendable_ids,
)
from dashboard.services.failed_case_export import stream_failed_case_csv
from dashboard.services.failed_case_filters import (
    FailedCaseFilters,
    FailedCaseView,
    encode_filters,
)
from dashboard.services.failed_case_groups import (
    FailingCaseRow,
    list_failing_test_cases,
)
from dashboard.services.failed_case_presets import (
    delete_preset,
    list_presets,
    save_preset,
)
from dashboard.services.failed_cases import (
    failed_case_run_choices,
    list_failed_cases,
)
from dashboard.services.member_projects import (
    member_project_choices,
    member_project_choices_and_writable_ids,
)
from projects.forms import form_error_text
from projects.models import TestRunTestCase


def _bind_filter_form(
    data: QueryDict, project_choices: Sequence[tuple[int, str]]
) -> FailedCaseFilterForm:
    """The validated filter form; run choices only for the selected project."""
    project_ids = [project_id for project_id, _ in project_choices]
    selected_project = FailedCaseFilterForm.selected_project_id(data, project_choices)
    run_choices = failed_case_run_choices(project_ids, selected_project)
    form = FailedCaseFilterForm(project_choices, run_choices, data)
    form.is_valid()
    return form


def _clean_query(project_choices: Sequence[tuple[int, str]], raw_query: str) -> str:
    """``raw_query`` re-validated against the user's projects and re-encoded
    (invalid fields dropped), so it is only ever a filter query string."""
    form = _bind_filter_form(QueryDict(raw_query), project_choices)
    return encode_filters(form.to_filters(), view=form.selected_view())


def _failures_redirect(query: str) -> HttpResponseRedirect:
    url = reverse("dashboard:failed_cases")
    return redirect(f"{url}?{query}" if query else url)


@login_required
@require_safe
def failed_cases(request: AuthenticatedRequest) -> HttpResponse:
    project_choices, writable_ids = member_project_choices_and_writable_ids(
        request.user
    )
    project_ids = [project_id for project_id, _ in project_choices]
    form = _bind_filter_form(request.GET, project_choices)
    filters: FailedCaseFilters = form.to_filters()
    view = form.selected_view()
    page = parse_page(request)
    per_page = parse_per_page(request)
    page_obj: Page[FailingCaseRow] | Page[TestRunTestCase]
    run_of_pivot: dict[int, int] = {}
    sendable_ids: list[int] = []
    no_source_ids: list[int] = []
    if view == FailedCaseView.BY_CASE:
        page_obj = list_failing_test_cases(
            project_ids, filters, page=page, per_page=per_page
        )
    else:
        cases_page = list_failed_cases(
            project_ids, filters, page=page, per_page=per_page
        )
        run_of_pivot = {pivot.id: pivot.test_run_id for pivot in cases_page}
        sendable_ids = failed_case_sendable_ids(cases_page, writable_ids)
        no_source_ids = failed_case_no_source_ids(cases_page)
        page_obj = cases_page

    filter_query = encode_filters(filters, view=view)
    cases_query = encode_filters(filters)
    by_case_query = encode_filters(filters, view=FailedCaseView.BY_CASE)
    base_url = reverse("dashboard:failed_cases")
    context: dict[str, object] = {
        "form": form,
        "filters": filters,
        "view": view,
        "page_obj": page_obj,
        "elided_page_range": elided_page_range(page_obj),
        "per_page": per_page,
        "allowed_per_page": ALLOWED_PER_PAGE,
        "query_params": f"&per_page={per_page}"
        + (f"&{filter_query}" if filter_query else ""),
        "filter_query": filter_query,
        "has_projects": bool(project_choices),
        "has_filters": bool(encode_filters(filters)),
        "cases_url": f"{base_url}?{cases_query}" if cases_query else base_url,
        "by_case_url": f"{base_url}?{by_case_query}",
        "export_url": reverse("dashboard:failed_cases_export")
        + (f"?{filter_query}" if filter_query else ""),
        "presets": list_presets(request.user),
        "preset_form": FailedCasePresetForm(initial={"query": filter_query}),
        "bulk_form": FailedCaseBulkForm(initial={"return_query": filter_query}),
        "selectable_ids": list(run_of_pivot),
        "run_of_pivot": run_of_pivot,
        "sendable_ids": sendable_ids,
        "no_source_ids": no_source_ids,
        "active_nav": "failures",
    }
    return render(request, "dashboard/failed_cases.html", context)


@login_required
@require_GET
def failed_cases_export(request: AuthenticatedRequest) -> HttpResponseBase:
    project_choices = member_project_choices(request.user)
    form = _bind_filter_form(request.GET, project_choices)
    if form.errors:
        messages.error(request, form_error_text(form))
        return _failures_redirect(request.GET.urlencode())
    filter_query = encode_filters(form.to_filters(), view=form.selected_view())
    try:
        check_export_throttle(request.user)
    except ExportThrottledError as exc:
        messages.error(request, exc.message)
        return _failures_redirect(filter_query)
    project_ids = [project_id for project_id, _ in project_choices]
    response = StreamingHttpResponse(
        stream_failed_case_csv(project_ids, form.to_filters()),
        content_type="text/csv; charset=utf-8",
    )
    filename = f"failed-cases-{timezone.now():%Y%m%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def failed_cases_bulk(request: AuthenticatedRequest) -> HttpResponse:
    form = FailedCaseBulkForm(request.POST)
    # Filters keep every member project; the action is scoped to the writable
    # ones, so cases of read-only projects count as skipped.
    project_choices, writable_ids = member_project_choices_and_writable_ids(
        request.user
    )
    return_query = _clean_query(project_choices, request.POST.get("return_query", ""))
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _failures_redirect(return_query)
    outcome = apply_failed_case_bulk(
        user=request.user,
        project_ids=writable_ids,
        action=form.selected_action(),
        pivot_ids=form.cleaned_data["pivot_ids"],
    )
    for text in outcome.successes:
        messages.success(request, text)
    for text in outcome.errors:
        messages.error(request, text)
    if outcome.skipped:
        noun = "case was" if outcome.skipped == 1 else "cases were"
        messages.info(
            request,
            f"{outcome.skipped} selected {noun} skipped: no longer a failed case "
            "in your projects, or in a read-only project.",
        )
    return _failures_redirect(return_query)


@login_required
@require_POST
def preset_save(request: AuthenticatedRequest) -> HttpResponse:
    form = FailedCasePresetForm(request.POST)
    query = _clean_query(
        member_project_choices(request.user), request.POST.get("query", "")
    )
    if not form.is_valid():
        messages.error(request, form_error_text(form))
        return _failures_redirect(query)
    try:
        preset = save_preset(request.user, form.cleaned_data["name"], query)
    except PresetLimitError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, f"Saved the filters as “{preset.name}”.")
    return _failures_redirect(query)


@login_required
@require_POST
def preset_delete(request: AuthenticatedRequest, preset_id: int) -> HttpResponse:
    try:
        delete_preset(request.user, preset_id)
    except PresetNotFoundError as exc:
        raise Http404 from exc
    messages.success(request, "Deleted the saved filter.")
    return _failures_redirect(
        _clean_query(
            member_project_choices(request.user),
            request.POST.get("return_query", ""),
        )
    )
