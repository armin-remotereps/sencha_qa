"""Prompt text and target extraction for the case teardown sub-task."""

from __future__ import annotations

from collections.abc import Sequence

from agents.checkpoint import final_step_indices
from agents.services.orchestrator_prompts import CLOSE_CLAUSE_PREFIX
from agents.types import SubTask, SubTaskAttempt

_CLOSE_CLAUSE_MAX_CHARS = 300


def pending_close_clauses(
    plan: Sequence[SubTask], attempts: Sequence[SubTaskAttempt]
) -> tuple[str, ...]:
    """Close clauses of the planned steps whose final attempt did not pass.

    Unrun steps count: their housekeeping never ran either.
    """
    final_index = final_step_indices(attempts)
    clauses: list[str] = []
    for step_num, task in enumerate(plan, start=1):
        index = final_index.get(step_num)
        if index is not None and attempts[index].result.status == "pass":
            continue
        start = task.description.lower().find(CLOSE_CLAUSE_PREFIX.lower())
        if start < 0:
            continue
        clause = " ".join(task.description[start:].split())
        clauses.append(clause[:_CLOSE_CLAUSE_MAX_CHARS])
    return tuple(clauses)


def failed_attempt_digests(
    attempts: Sequence[SubTaskAttempt], *, limit: int = 3
) -> tuple[str, ...]:
    """Action digests of the last ``limit`` failed attempts, oldest first."""
    failed = [
        attempt.result.actions_digest
        for attempt in attempts
        if attempt.result.status == "fail" and attempt.result.actions_digest
    ]
    return tuple(failed[-limit:]) if limit > 0 else ()


def build_teardown_sub_task(
    close_clauses: Sequence[str], digests: Sequence[str], timeout_seconds: int
) -> SubTask:
    targets = (
        "\n".join(f"- {clause}" for clause in close_clauses)
        if close_clauses
        else "- (none listed; use the action logs below)"
    )
    logs = (
        "\n\n".join(
            f"Failed attempt {number}:\n{digest}"
            for number, digest in enumerate(digests, start=1)
        )
        if digests
        else "(none recorded)"
    )
    description = (
        "HOUSEKEEPING ONLY. This test case has already ended and its result is "
        "decided. Do not test, judge, verify or redo anything; only close the desktop "
        "applications and tabs this case left open.\n\n"
        "Close instructions from steps that never completed:\n"
        f"{targets}\n\n"
        "Actions the case's failed attempts took (shows what they opened, e.g. "
        f"launch_app or clicks):\n{logs}\n\n"
        "How:\n"
        "1. Take one screenshot first to see what is open.\n"
        "2. Close only applications and tabs that are listed above or that the "
        "action logs show this case opened, and that are still open. Skip an "
        "application the case's steps never reached.\n"
        "3. Use close_app for an application (force=true is allowed: unsaved work of "
        "a finished case may be discarded). For a tab, click the tab this case "
        "opened to bring it to the front, then use close_tab.\n"
        "4. Use click only to focus such a tab or to answer a save or quit dialog "
        'with "Don\'t Save" / "Discard".\n'
        "Never close the Test Runner, its terminal, the operating system shell, the "
        "automation browser (it is closed automatically) or anything not described "
        "above. If nothing is left open, finish straight away."
    )
    return SubTask(
        description=description,
        expected_result=(
            "The listed applications and tabs opened by this case are closed; "
            "nothing else was closed."
        ),
        timeout_seconds=timeout_seconds,
    )
