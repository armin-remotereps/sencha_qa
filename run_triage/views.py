"""Case analysis report page and its CSV export (thin views over read services)."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_safe

from accounts.types import AuthenticatedRequest
from projects.decorators import project_membership_required
from projects.models import Project, TestRun
from run_triage.exceptions import RunTriageError
from run_triage.markdown_export import triage_markdown
from run_triage.services.export_throttle import allow_triage_export
from run_triage.services.report import get_triage_report
from run_triage.services.run_lookup import get_run_for_project
from run_triage.services.triage_export import triage_csv_filename, triage_csv_rows


def _run_of_project(project: Project, run_id: int) -> TestRun:
    try:
        return get_run_for_project(project, run_id)
    except RunTriageError as exc:
        raise Http404 from exc


@project_membership_required
@require_safe
def report(
    request: AuthenticatedRequest, project: Project, run_id: int
) -> HttpResponse:
    test_run = _run_of_project(project, run_id)
    triage_report = get_triage_report(test_run, project)
    thread_url = (
        reverse(
            "machine_setup:thread",
            kwargs={
                "project_id": project.id,
                "conversation_id": triage_report.actions.thread_id,
            },
        )
        if triage_report.actions.thread_id is not None
        else ""
    )
    return render(
        request,
        "run_triage/report.html",
        {
            "project": project,
            "test_run": test_run,
            "report": triage_report,
            "markdown": triage_markdown(triage_report),
            "thread_url": thread_url,
            "card": triage_report.actions,
            "active_tab": "runs",
            "active_nav": "projects",
        },
    )


@project_membership_required
@require_safe
def export_csv(
    request: AuthenticatedRequest, project: Project, run_id: int
) -> HttpResponseBase:
    test_run = _run_of_project(project, run_id)
    if not allow_triage_export(request.user.id):
        messages.error(
            request, "Too many downloads in a short time. Try again in a minute."
        )
        return redirect("run_triage:report", project_id=project.id, run_id=test_run.id)
    # Bounded by RUN_TRIAGE_MAX_CASES and built from the report already in
    # memory, so one response body (a sync iterator would be buffered under
    # ASGI anyway).
    response = HttpResponse(
        "".join(triage_csv_rows(test_run, project)),
        content_type="text/csv; charset=utf-8",
    )
    filename = triage_csv_filename(test_run.id)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response
