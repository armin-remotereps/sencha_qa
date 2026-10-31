"""Evidence for a person's Mark failed / Mark blocked (Human in the loop).

The person's decision is final; before ``projects`` writes it, the worker
lets the agent record what the machine shows: a screenshot and, with
``PERSON_EVIDENCE_LLM``, one read-only check, as a failed person attempt at
the marked step. Knows nothing of the ORM: it takes and returns a checkpoint.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from django.conf import settings
from django.utils import timezone

from agents.checkpoint import person_mark_verdict
from agents.services.llm_config import build_llm_config
from agents.services.person_step_prompts import format_mark_summary
from agents.services.person_steps import person_failure_result, record_mark_evidence
from agents.types import (
    AgentConfig,
    AttemptScreenshotCallback,
    BrowserSelection,
    CancellationCheck,
    CheckpointPersonStep,
    LogCallback,
    OrchestratorCheckpoint,
    ScreenshotCallback,
    SubTaskAttempt,
)


def record_person_mark(
    checkpoint: OrchestratorCheckpoint,
    project_id: int,
    *,
    on_log: LogCallback | None,
    on_screenshot: AttemptScreenshotCallback | None,
    system_info: dict[str, object] | None,
    project_prompt: str | None,
    cancellation_check: CancellationCheck | None,
) -> OrchestratorCheckpoint:
    """``checkpoint`` with the evidence attempt of its marked person step
    appended and the ``person`` key cleared; the cursor is unchanged.

    Raises ``ValueError`` when the ``person`` key is not a mark; faults
    (runner lost, AI service, cancellation) raise as for any sub-agent.
    """
    person = checkpoint.person
    verdict = person_mark_verdict(person)
    if person is None or verdict is None:
        raise ValueError("The checkpoint has no person mark")
    hand_off = person.hand_off
    step_num = hand_off.step_num
    llm = build_llm_config()
    attempt_id = uuid.uuid4().hex
    if on_log is not None:
        on_log(
            f"[Orchestrator]   Recording evidence: a person marked the case {verdict} "
            f"at step {step_num}"
        )
    check = record_mark_evidence(
        person,
        project_id,
        verdict=verdict,
        config=AgentConfig(
            llm=llm,
            vision_llm=llm,
            max_iterations=settings.SUB_AGENT_MAX_ITERATIONS,
            on_log=on_log,
            on_screenshot=_tagged(on_screenshot, attempt_id, step_num),
            cancellation_check=cancellation_check,
        ),
        browser_selection=BrowserSelection(current=checkpoint.browser),
        system_info=system_info,
        project_prompt=project_prompt,
        state_description=f"Step {step_num} of {len(checkpoint.plan)}.",
    )
    result = person_failure_result(
        format_mark_summary(
            person.reporter,
            verdict,
            person.note,
            check.result.summary,
            output_attached=bool(person.output.strip()),
        ),
        person,
        check,
        step_num=step_num,
        on_log=on_log,
    )
    attempt = SubTaskAttempt(
        step_num=step_num,
        kind="resume" if hand_off.kind == "restore" else "step",
        result=result,
        duration_seconds=_duration(person),
        attempt_id=attempt_id,
        performed_by="person",
        person_step_id=person.request_id,
    )
    return replace(
        checkpoint,
        attempts=(*checkpoint.attempts, attempt),
        total_iterations=checkpoint.total_iterations + result.iterations,
        person=None,
        phase_started_at=timezone.now(),
    )


def _tagged(
    on_screenshot: AttemptScreenshotCallback | None, attempt_id: str, step_num: int
) -> ScreenshotCallback | None:
    if on_screenshot is None:
        return None
    case_callback = on_screenshot

    def forward(base64_data: str, tool_name: str) -> None:
        case_callback(base64_data, tool_name, attempt_id=attempt_id, step_num=step_num)

    return forward


def _duration(person: CheckpointPersonStep) -> int | None:
    """Handed out to marked (``reported_at`` is the mark's time)."""
    if person.requested_at is None or person.reported_at is None:
        return None
    seconds = (person.reported_at - person.requested_at).total_seconds()
    return max(0, int(round(seconds)))
