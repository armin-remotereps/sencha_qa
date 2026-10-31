"""Pause rules and texts, and the continue modes a help request allows.

Pure: no ORM and no import of ``projects.services``, so the worker, the help
services, the pause services and machine setup can all use it.
"""

from __future__ import annotations

from enum import StrEnum

from agents.checkpoint import checkpoint_from_json
from agents.types import OrchestratorCheckpoint
from projects.models import BlockedReason, HelpContinueMode

# Faults after which the screen cannot be trusted (``case_faults``): the
# runner may have restarted, so a case re-running its step there would test
# the wrong state.
_UNTRUSTED_SCREEN_FAULTS = frozenset(
    {
        BlockedReason.RUNNER_DISCONNECTED,
        BlockedReason.RUNNER_UNRESPONSIVE,
        BlockedReason.RUNNER_OUTDATED,
    }
)

# Display order after the stored mode.
_MODE_ORDER: tuple[HelpContinueMode, ...] = (
    HelpContinueMode.CURRENT_SCREEN,
    HelpContinueMode.RESTORE,
    HelpContinueMode.FROM_SCRATCH,
)

PAUSE_NOT_RUNNING = "This case is not running, so it cannot be paused."
PAUSE_ALREADY_NEEDS_HELP = (
    "This case already waits for help. Open the help chat to discuss it."
)
PAUSE_RUN_NOT_ACTIVE = "This run is not running, so its cases cannot be paused."
PAUSE_STEP_BEING_CONFIRMED = (
    "The agent is checking the reported step. Try again in a moment."
)
PAUSE_SECOND_MESSAGE = (
    "A pause was already requested with a message; yours was not sent. "
    "Write it in the discussion once it opens."
)
PAUSE_NOTHING_TO_DISMISS = "There is no pause notice to dismiss."
REDESIGN_NEEDS_CURRENT_SCREEN = (
    "A redesigned plan continues from the current screen. Choose that mode, "
    "or continue without the redesign."
)
REDESIGN_RESTORE_FIRST = (
    "Restore first: the screen cannot be trusted, so a redesigned plan cannot "
    "continue from it."
)
# Query parameter of a case page that follows the paused case's discussion
# as soon as it is open (set by the pause view and the page's own reload).
AWAIT_DISCUSSION_PARAM = "discussion"
# Cap of the server-written pauser labels (``pause_requested_label``,
# ``paused_by_label``).
PAUSE_LABEL_MAX_LENGTH = 255
MODE_NOT_ALLOWED = "That way of continuing is not allowed for this case."


def pause_reason(label: str, step_num: int) -> str:
    """The help request's reason for a person's pause."""
    return f"Paused by {label} at step {step_num} to discuss the case."


def pause_ask() -> str:
    """The help request's ask for a person's pause."""
    return (
        "Discuss the case with the agent, change the plan or the machine if "
        "needed, then Continue."
    )


def pause_log_line(label: str, step_num: int) -> str:
    return f"[Paused by {label} at step {step_num}]"


def pause_continue_mode(
    checkpoint: OrchestratorCheckpoint | None, *, restore_pending: bool
) -> HelpContinueMode:
    """How a case a person paused goes on by default (stored on its request).

    No checkpoint: from scratch. A restore due and not yet finished: restore.
    Otherwise the step re-runs from the current screen.
    """
    if checkpoint is None:
        return HelpContinueMode.FROM_SCRATCH
    if restore_pending:
        return HelpContinueMode.RESTORE
    return HelpContinueMode.CURRENT_SCREEN


def allowed_continue_modes(
    stored: HelpContinueMode | str, fault: str, checkpoint_json: object
) -> tuple[HelpContinueMode, ...]:
    """The modes Continue may use for a help request, stored mode first.

    - from scratch: always (it clears the checkpoint and cleans up first);
    - restore: with a checkpoint whose cursor is past step 1 and within the
      plan, or when it is the stored mode (a runner fault at step 1 means
      cleanup, then step 1);
    - current screen: with a checkpoint, unless the stored mode is restore or
      the fault lost the runner (the screen cannot be trusted);
    - no checkpoint: only from scratch.
    """
    stored_mode = HelpContinueMode(stored)
    checkpoint = checkpoint_from_json(checkpoint_json)
    allowed: set[HelpContinueMode] = {HelpContinueMode.FROM_SCRATCH}
    if checkpoint is not None:
        if stored_mode == HelpContinueMode.RESTORE or 1 < checkpoint.next_step <= len(
            checkpoint.plan
        ):
            allowed.add(HelpContinueMode.RESTORE)
        if (
            stored_mode != HelpContinueMode.RESTORE
            and fault not in _UNTRUSTED_SCREEN_FAULTS
        ):
            allowed.add(HelpContinueMode.CURRENT_SCREEN)
    ordered = [stored_mode] if stored_mode in allowed else []
    ordered += [mode for mode in _MODE_ORDER if mode in allowed and mode != stored_mode]
    return tuple(ordered)


class CasePauseOutcome(StrEnum):
    """What ``case_pause_services.request_case_pause`` did."""

    # Stored: the worker stops at the agent's next action.
    REQUESTED = "requested"
    # A pause was already requested for this run of the case; the first
    # requester (and message) is kept.
    ALREADY_REQUESTED = "already_requested"
    # A waiting person step was converted at once: the case needs help.
    PAUSED = "paused"
