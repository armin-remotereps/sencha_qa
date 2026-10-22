"""Persist and read a run case's orchestrator checkpoint (resume in place).

The orchestrator stays ORM-free and reports its progress through a callback;
this module stores it on ``TestRunTestCase.execution_checkpoint`` and turns it
back into an ``OrchestratorCheckpoint`` (or step choices for the case page).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from agents.checkpoint import (
    checkpoint_from_json,
    checkpoint_to_json,
)
from agents.types import CheckpointCallback, OrchestratorCheckpoint
from projects.models import TestRunTestCase


@dataclass(frozen=True)
class ResumeStepOption:
    """One step a case can resume at; ``passed`` is its final step attempt.

    ``verdict_only`` marks the extra option after the last step (every step
    already ran; resuming only builds the verdict).
    """

    step_num: int
    description: str
    passed: bool
    verdict_only: bool = False


def build_checkpoint_callback(
    pivot: TestRunTestCase, generation: int
) -> CheckpointCallback:
    """Write each reported checkpoint to the pivot row (one small UPDATE).

    The write only lands while the run still has ``generation``, so a worker a
    Resume or Start took over cannot overwrite the newer checkpoint.
    """
    rows = TestRunTestCase.objects.filter(
        pk=pivot.pk, test_run__execution_generation=generation
    )

    def _save_checkpoint(checkpoint: OrchestratorCheckpoint) -> None:
        data = checkpoint_to_json(checkpoint)
        pivot.execution_checkpoint = data
        rows.update(execution_checkpoint=data, updated_at=timezone.now())

    return _save_checkpoint


def load_resume_checkpoint(pivot: TestRunTestCase) -> OrchestratorCheckpoint | None:
    """The pivot's checkpoint, or ``None`` to plan from scratch."""
    return checkpoint_from_json(pivot.execution_checkpoint)


def list_resume_steps(pivot: TestRunTestCase) -> list[ResumeStepOption]:
    """Steps 1..cursor of the loaded pivot's checkpoint (no query).

    The last option is the cursor, the default; when every step already ran it
    is a ``verdict_only`` option one past the last step.
    """
    checkpoint = load_resume_checkpoint(pivot)
    if checkpoint is None:
        return []
    plan_length = len(checkpoint.plan)
    final_status: dict[int, str] = {}
    for attempt in checkpoint.attempts:
        if attempt.kind == "step":
            final_status[attempt.step_num] = attempt.result.status
    options = [
        ResumeStepOption(
            step_num=step_num,
            description=checkpoint.plan[step_num - 1].description,
            passed=final_status.get(step_num) == "pass",
        )
        for step_num in range(1, min(checkpoint.next_step, plan_length) + 1)
    ]
    if checkpoint.next_step > plan_length:
        options.append(
            ResumeStepOption(
                step_num=checkpoint.next_step,
                description="",
                passed=False,
                verdict_only=True,
            )
        )
    return options
