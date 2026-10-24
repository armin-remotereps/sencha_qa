"""Close the desktop apps and tabs a finished, unsuccessful case left open.

A passing case closes them itself: the planner ends the last step using each app
with a close clause. A case that failed or was blocked never reaches those
clauses, so this runs a restricted sub-agent on what is left. It is housekeeping:
it records no attempt, writes no checkpoint and never changes the outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from agents.services.case_teardown_prompts import (
    build_teardown_sub_task,
    failed_attempt_digests,
    pending_close_clauses,
)
from agents.services.llm_config import build_llm_config
from agents.services.sub_agent import run_sub_agent
from agents.types import (
    AgentCancelledError,
    AgentConfig,
    BrowserSelection,
    CancellationCheck,
    LogCallback,
    OrchestratorCheckpoint,
)

logger = logging.getLogger(__name__)

# No key_press (a quit shortcut hits whatever has focus, the runner included)
# and no typing, shell, launch or browser tools: those are test actions.
TEARDOWN_TOOL_NAMES: Final = frozenset(
    {"take_screenshot", "close_app", "close_tab", "click", "wait"}
)
_TEARDOWN_MAX_ITERATIONS: Final = 30


def run_case_teardown(
    checkpoint: OrchestratorCheckpoint,
    project_id: int,
    *,
    timeout_seconds: int,
    on_log: LogCallback | None,
    cancellation_check: CancellationCheck | None,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    machine_ready: Callable[[], bool],
) -> None:
    """Best effort: every ``Exception`` is logged, never raised.

    ``machine_ready`` is asked only once there is something to close, so a case
    with no targets costs no query.
    """
    try:
        _run(
            checkpoint,
            project_id,
            machine_ready=machine_ready,
            timeout_seconds=timeout_seconds,
            on_log=on_log,
            cancellation_check=cancellation_check,
            system_info=system_info,
            project_prompt=project_prompt,
        )
    except AgentCancelledError:
        _log(on_log, "[Teardown] Stopped: the run was aborted or taken over")
    except Exception as exc:
        logger.warning("Case teardown for project %d failed", project_id, exc_info=True)
        _log(on_log, f"[Teardown] Skipped: {type(exc).__name__}")


def _run(
    checkpoint: OrchestratorCheckpoint,
    project_id: int,
    *,
    machine_ready: Callable[[], bool],
    timeout_seconds: int,
    on_log: LogCallback | None,
    cancellation_check: CancellationCheck | None,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
) -> None:
    clauses = pending_close_clauses(checkpoint.plan, checkpoint.attempts)
    digests = failed_attempt_digests(checkpoint.attempts)
    if not clauses and not any("launch_app(" in digest for digest in digests):
        _log(on_log, "[Teardown] Nothing left open to close")
        return
    if not machine_ready():
        _log(on_log, "[Teardown] Skipped: the test machine cannot close apps now")
        return

    targets = "; ".join(clauses) if clauses else "apps launched by failed attempts"
    _log(
        on_log,
        f"[Teardown] Closing what this case left open (budget {timeout_seconds}s): "
        f"{targets}",
    )
    llm = build_llm_config()
    config = AgentConfig(
        llm=llm,
        vision_llm=llm,
        max_iterations=_TEARDOWN_MAX_ITERATIONS,
        timeout_seconds=timeout_seconds,
        on_log=on_log,
        # Teardown screenshots are not evidence of the case.
        on_screenshot=None,
        cancellation_check=cancellation_check,
    )
    result = run_sub_agent(
        build_teardown_sub_task(clauses, digests, timeout_seconds),
        "The test case has finished; the task lists what it opened.",
        project_id,
        config=config,
        browser_selection=BrowserSelection(current=checkpoint.browser),
        system_info=system_info,
        project_prompt=project_prompt,
        tool_names=TEARDOWN_TOOL_NAMES,
    )
    _log(on_log, f"[Teardown] Result: {result.status.upper()} — {result.summary}")


def _log(on_log: LogCallback | None, message: str) -> None:
    logger.info(message)
    if on_log is not None:
        on_log(message)
