"""Shared types of a delegated setup sub-task (main agent → sub-agent).

Pure: no ORM, no settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agents.types import ToolCall, ToolContext, ToolResult

RUN_SETUP_TASK_TOOL = "run_setup_task"

GOAL_MAX_CHARS = 2000
DONE_WHEN_MAX_CHARS = 1000
KNOWN_STATE_MAX_CHARS = 6000


@dataclass(frozen=True)
class SetupTaskRequest:
    goal: str
    done_when: str
    known_state: str
    # Raw model value; clamped by subtask_budget.resolve_subtask_budget.
    requested_timeout: object
    read_only: bool


# Runs one delegated sub-task for the main agent's run_setup_task call.
SubtaskRunner = Callable[[ToolCall, SetupTaskRequest, ToolContext], ToolResult]
