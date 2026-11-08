"""Use case: the MCP client lists the projects its connection may use
(list_projects).

OAuth: the grant's live project links (``oauth_access.live_links``), by
project name; a link whose project's Connection key changed since it was
made is left out (compared in Python, constant time). Connection key: the
key's one project, so a key client learns the ``project_id`` it must pass.
Each project comes with its running Manual run (newest started; None when
none runs). One query. No capture readiness here: that is a runner round
trip per project, so ``get_project`` keeps it.
"""

from __future__ import annotations

from django.db.models import OuterRef, QuerySet, Subquery

from mcp_gateway.oauth_types import McpCaller
from mcp_gateway.schemas import ListProjectsResponse, ProjectRef, RunRef
from mcp_gateway.services.oauth_access import link_fingerprint_matches, live_links
from mcp_gateway.untrusted_text import case_line
from projects.models import Project, RunHelpMode, TestRun, TestRunStatus


def _running_manual_runs(project_ref: OuterRef) -> QuerySet[TestRun]:
    """The running Manual runs of ``project_ref``, newest started first."""
    return TestRun.objects.filter(
        project_id=project_ref,
        help_mode=RunHelpMode.MANUAL,
        status=TestRunStatus.STARTED,
    ).order_by("-started_at", "-pk")


def _project_ref(
    project_id: int, name: str, run_id: int | None, run_name: str | None
) -> ProjectRef:
    return ProjectRef(
        project_id=project_id,
        name=case_line(name),
        manual_run=(
            RunRef(id=run_id, name=case_line(run_name or f"Run #{run_id}"))
            if run_id is not None
            else None
        ),
    )


def list_projects(caller: McpCaller) -> ListProjectsResponse:
    if caller.grant_id is not None:
        runs = _running_manual_runs(OuterRef("project_id"))
        links = (
            live_links()
            .filter(grant_id=caller.grant_id)
            .annotate(
                run_id=Subquery(runs.values("id")[:1]),
                run_name=Subquery(runs.values("name")[:1]),
            )
            .only(
                "api_key_fingerprint",
                "project",
                "project__id",
                "project__name",
                "project__api_key",
            )
            .order_by("project__name", "project_id")
        )
        return ListProjectsResponse(
            projects=[
                _project_ref(
                    link.project.pk, link.project.name, link.run_id, link.run_name
                )
                for link in links
                if link_fingerprint_matches(link)
            ]
        )
    if caller.key_project_id is None:
        return ListProjectsResponse(projects=[])
    runs = _running_manual_runs(OuterRef("pk"))
    rows = (
        Project.objects.filter(pk=caller.key_project_id, archived=False)
        .annotate(
            run_id=Subquery(runs.values("id")[:1]),
            run_name=Subquery(runs.values("name")[:1]),
        )
        .values_list("pk", "name", "run_id", "run_name")
    )
    return ListProjectsResponse(
        projects=[
            _project_ref(pk, name, run_id, run_name)
            for pk, name, run_id, run_name in rows
        ]
    )
