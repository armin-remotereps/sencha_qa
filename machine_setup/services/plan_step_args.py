"""Arguments of the plan redesign tools (help chat and result chat).

Shared by ``help_chat_tools`` and ``result_chat_tools``: the JSON schema of one
step of ``propose_plan_change``, and the parsers of its arguments. A parse
error raises ``PlanStepArgumentError``, whose message goes back to the model
as a tool error.
"""

from __future__ import annotations

from django.conf import settings

from agents.plan_revision import (
    MAX_DESCRIPTION_CHARS,
    MAX_EXPECTED_RESULT_CHARS,
    PlanStepInput,
)


class PlanStepArgumentError(Exception):
    """A tool argument the model must fix; the message goes back to it."""


STEP_ITEM_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "What to do, naming the application, window or terminal (at most "
                f"{MAX_DESCRIPTION_CHARS} characters)."
            ),
        },
        "expected_result": {
            "type": "string",
            "description": (
                "What must be true afterwards (at most "
                f"{MAX_EXPECTED_RESULT_CHARS} characters)."
            ),
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Optional time budget in seconds; clamped to the limits.",
        },
        "carried_from": {
            "type": "integer",
            "description": (
                "Optional. The earlier step number this step is carried from "
                "(its final attempt passed). Omit for a step that will run."
            ),
        },
        "case_steps": {
            "type": "array",
            "items": {"type": "integer"},
            "description": (
                "Optional. The test case's numbered steps (1-based) this step "
                "performs or checks. Omit for a carried step to keep its earlier "
                "numbers."
            ),
        },
    },
    "required": ["description", "expected_result"],
}


def parse_plan_steps(value: object) -> list[PlanStepInput]:
    if not isinstance(value, list):
        raise PlanStepArgumentError(
            "steps is required and must be a list of step objects."
        )
    max_steps = settings.ORCHESTRATOR_MAX_SUBTASKS
    if not value:
        raise PlanStepArgumentError("steps is empty. Give the complete new step list.")
    if len(value) > max_steps:
        raise PlanStepArgumentError(
            f"steps has {len(value)} items; the limit is {max_steps}. Merge steps."
        )
    steps: list[PlanStepInput] = []
    for num, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise PlanStepArgumentError(f"Step {num} must be an object.")
        description = raw.get("description")
        expected = raw.get("expected_result")
        if not isinstance(description, str):
            raise PlanStepArgumentError(f"Step {num}: description must be a string.")
        if not isinstance(expected, str):
            raise PlanStepArgumentError(
                f"Step {num}: expected_result must be a string."
            )
        timeout = raw.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool)
        ):
            raise PlanStepArgumentError(
                f"Step {num}: timeout_seconds must be an integer."
            )
        carried = raw.get("carried_from")
        if carried is not None and (
            not isinstance(carried, int) or isinstance(carried, bool) or carried < 1
        ):
            raise PlanStepArgumentError(
                f"Step {num}: carried_from must be an earlier step number (1 or "
                "more), or omitted."
            )
        case_steps = raw.get("case_steps")
        if case_steps is not None and (
            not isinstance(case_steps, list)
            or any(
                not isinstance(ref, int) or isinstance(ref, bool) for ref in case_steps
            )
        ):
            # Out-of-range numbers are dropped later, never an error.
            raise PlanStepArgumentError(
                f"Step {num}: case_steps must be a list of integers, or omitted."
            )
        steps.append(
            PlanStepInput(
                description=description,
                expected_result=expected,
                raw_timeout=timeout,
                carried_from=carried,
                case_steps=case_steps if case_steps is not None else (),
            )
        )
    return steps


def required_text(arguments: dict[str, object], name: str, limit: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise PlanStepArgumentError(f"{name} is required and must be a string.")
    value = value.strip()
    if not value:
        raise PlanStepArgumentError(f"{name} is empty.")
    if len(value) > limit:
        raise PlanStepArgumentError(
            f"{name} is {len(value)} characters; the limit is {limit}. Shorten it "
            "and call again."
        )
    return value
