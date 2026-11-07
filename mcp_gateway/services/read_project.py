"""Use case: the MCP tester reads its project (get_project).

The project's name, its running Manual run (if any), the screenshot limits
and the workflow text. One query.
"""

from __future__ import annotations

from django.conf import settings

from controller_client.protocol import MANUAL_SHOT_MAX_PIXELS, MANUAL_STEP_MAX_SHOTS
from mcp_gateway.limits import MCP_STAGED_SHOTS_MAX
from mcp_gateway.schemas import ProjectResponse, RunRef
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.untrusted_text import case_line
from mcp_gateway.workflow import WORKFLOW_TEXT
from projects.models import RunHelpMode, TestRun, TestRunStatus


def read_project(call: McpCall) -> ProjectResponse:
    run = (
        TestRun.objects.filter(
            project_id=call.project.pk,
            help_mode=RunHelpMode.MANUAL,
            status=TestRunStatus.STARTED,
        )
        .order_by("-started_at", "-pk")
        .values_list("id", "name")
        .first()
    )
    return ProjectResponse(
        name=case_line(call.project.name),
        manual_run=(
            RunRef(id=run[0], name=case_line(run[1] or f"Run #{run[0]}"))
            if run is not None
            else None
        ),
        step_max_shots=MANUAL_STEP_MAX_SHOTS,
        shot_max_bytes=settings.MANUAL_SHOT_MAX_BYTES,
        shot_max_pixels=MANUAL_SHOT_MAX_PIXELS,
        staged_shots_max=MCP_STAGED_SHOTS_MAX,
        how_to=WORKFLOW_TEXT,
    )
