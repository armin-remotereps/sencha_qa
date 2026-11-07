"""Use case: the MCP tester reads its project (get_project).

The project's name, its running Manual run (if any), the screenshot limits,
whether the test machine can capture screenshots now and the workflow text.
Two queries.
"""

from __future__ import annotations

from django.conf import settings

from controller_client.protocol import MANUAL_SHOT_MAX_PIXELS, MANUAL_STEP_MAX_SHOTS
from mcp_gateway.limits import MCP_STAGED_SHOTS_MAX
from mcp_gateway.schemas import CaptureInfo, ProjectResponse, RunRef
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.capture_errors import readiness_code
from mcp_gateway.untrusted_text import case_line
from mcp_gateway.workflow import WORKFLOW_TEXT
from projects.models import RunHelpMode, TestRun, TestRunStatus
from projects.runner_capture_readiness import capture_readiness


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
    readiness = capture_readiness(call.project.pk)
    reason = readiness_code(readiness)
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
        capture=CaptureInfo(
            available=reason is None,
            reason_code=None if reason is None else reason.value,
            allowed_processes=list(readiness.allowed),
        ),
        how_to=WORKFLOW_TEXT,
    )
