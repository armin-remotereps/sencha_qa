"""Settings-bound limits for plan revisions (sub-task timeouts, step count).

A leaf module: it imports only Django settings, ``agents.timeout_bounds``,
``agents.plan_revision`` and its types, so ``projects`` modules may use it without importing
``agents.services`` (that package imports ``projects.services``).
"""

from __future__ import annotations

from collections.abc import Sequence

from django.conf import settings

from agents.plan_revision import (
    PlanStepInput,
    ValidatedRevision,
    validate_plan_revision,
)
from agents.timeout_bounds import TimeoutBounds
from agents.types import OrchestratorCheckpoint


def build_timeout_bounds() -> TimeoutBounds:
    return TimeoutBounds(
        default=settings.SUB_AGENT_TIMEOUT_SECONDS,
        minimum=settings.SUB_AGENT_MIN_TIMEOUT_SECONDS,
        maximum=settings.SUB_AGENT_MAX_TIMEOUT_SECONDS,
    )


def validate_case_plan_revision(
    checkpoint: OrchestratorCheckpoint, steps: Sequence[PlanStepInput]
) -> ValidatedRevision:
    """Validate a redesign against the configured timeout and step limits."""
    return validate_plan_revision(
        checkpoint,
        steps,
        bounds=build_timeout_bounds(),
        max_steps=settings.ORCHESTRATOR_MAX_SUBTASKS,
    )
