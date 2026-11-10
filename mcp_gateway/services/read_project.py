"""Use case: the MCP tester reads its project (get_project).

The project's name, its running Manual run (if any), the screenshot limits,
whether the test machine can capture screenshots now, the test machine's
name, state, target, connection and browse_* browsers (``machine``) and the
workflow text. The test machine is the runner of the running Manual run
(docs/plan/feat-user-test-runners.md A16); without one, ``machine.name`` is
None and its state ``not_set_up``. Four queries.
"""

from __future__ import annotations

from typing import Final

from django.conf import settings

from controller_client.protocol import MANUAL_SHOT_MAX_PIXELS, MANUAL_STEP_MAX_SHOTS
from mcp_gateway.limits import MCP_STAGED_SHOTS_MAX
from mcp_gateway.schemas import CaptureInfo, MachineInfo, ProjectResponse, RunRef
from mcp_gateway.services.call_guard import McpCall
from mcp_gateway.services.capture_errors import CaptureErrorCode, readiness_code
from mcp_gateway.services.run_machine import load_manual_run_machine, machine_label
from mcp_gateway.untrusted_text import case_line
from mcp_gateway.workflow import WORKFLOW_TEXT
from projects.runner_capability_services import os_name_of, web_browsers_supported
from projects.runner_capture_readiness import capture_readiness
from projects.runner_target import runner_target_of
from runners.machine_state import runner_machine_state
from visual_engine.services.pool_status import get_visual_engine_status

# ``machine.state`` when the running Manual run has no test machine.
NO_MACHINE_STATE: Final = "not_set_up"


def read_project(call: McpCall) -> ProjectResponse:
    run = load_manual_run_machine(call.project.pk)
    runner = run.runner if run is not None else None
    readiness = capture_readiness(
        call.project.pk, runner.runner_id if runner is not None else None
    )
    reason = readiness_code(readiness)
    if runner is None and readiness.allowed:
        # No test machine to capture with (the same code capture_screenshot
        # answers).
        reason = CaptureErrorCode.NOT_SET_UP
    target = runner_target_of(runner)
    web_browsers = web_browsers_supported(
        target, os_name_of(runner.system_info if runner is not None else None)
    )
    return ProjectResponse(
        name=case_line(call.project.name),
        manual_run=(
            RunRef(id=run.run_id, name=case_line(run.run_name or f"Run #{run.run_id}"))
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
        machine=MachineInfo(
            name=machine_label(runner.name) if runner is not None else None,
            state=(
                runner_machine_state(runner, get_visual_engine_status()).value
                if runner is not None
                else NO_MACHINE_STATE
            ),
            target=target.value,
            connected=readiness.connected,
            web_browsers=[browser.value for browser in web_browsers],
        ),
        how_to=WORKFLOW_TEXT,
    )
