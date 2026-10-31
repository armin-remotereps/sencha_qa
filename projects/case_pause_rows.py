"""Write the help request a person's pause opens (or joins).

A leaf (models, ``case_pause_policy`` and ``agents.types`` only), so the
worker (``projects.services._pause_run_for_help`` / ``_pause_run_for_person``)
and ``case_pause_services.request_case_pause`` (a waiting person step) write
the same rows. Callers hold the run lock and the case lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agents.types import HelpRequestInfo
from projects.case_pause_policy import pause_ask, pause_reason
from projects.models import (
    PAUSE_REQUEST_CLEARED,
    CaseHelpRequest,
    HelpContinueMode,
    HelpResolution,
    PauseMessageState,
    TestRunTestCase,
    TestRunTestCaseStatus,
)


@dataclass(frozen=True)
class PauseRequestSnapshot:
    """A case's pending pause request, read under the case lock."""

    user_id: int | None
    # Server-written actor label; never runner-reported text.
    label: str
    via: str
    message: str


def read_pause_request(
    pivot: TestRunTestCase, generation: int
) -> PauseRequestSnapshot | None:
    """The loaded case's pause request for ``generation``; None when none."""
    if pivot.pause_requested_generation != generation:
        return None
    return PauseRequestSnapshot(
        user_id=pivot.pause_requested_by_id,
        label=pivot.pause_requested_label,
        via=pivot.pause_requested_via,
        message=pivot.pause_message,
    )


def clear_pause_request(pivot: TestRunTestCase) -> None:
    """Clear the case's pause fields in memory; save with ``PAUSE_REQUEST_FIELDS``."""
    for field, value in PAUSE_REQUEST_CLEARED.items():
        setattr(pivot, field, value)


def open_pause_help_request(
    pivot: TestRunTestCase,
    *,
    step_num: int,
    continue_mode: HelpContinueMode,
    requested: PauseRequestSnapshot,
    stopped_by_pause: bool,
    base: HelpRequestInfo | None,
    now: datetime,
    fault: str = "",
) -> CaseHelpRequest:
    """Open the case's help request with the pauser recorded, and save it.

    ``stopped_by_pause``: the pause itself stopped the case, so the reason
    and ask are the pause texts. Otherwise the agent's need_help or a fault
    won the race: ``base`` keeps its texts (and ``fault`` its fault), and the
    pauser still gets the discussion. Closes a leftover open request first
    (one open request per case) and clears the case's pause fields in memory
    (the caller saves them with the case).
    """
    CaseHelpRequest.objects.filter(
        test_run_test_case_id=pivot.pk, resolution=HelpResolution.OPEN
    ).update(resolution=HelpResolution.CANCELLED, resolved_at=now)
    step_num = max(step_num, 1)
    if stopped_by_pause or base is None:
        reason = pause_reason(requested.label, step_num)
        ask = pause_ask()
    else:
        reason, ask = base.reason, base.ask
    help_request = CaseHelpRequest.objects.create(
        test_run_test_case=pivot,
        step_num=step_num,
        reason=reason,
        ask=ask,
        fault="" if stopped_by_pause else fault,
        continue_mode=continue_mode,
        stopped_by_pause=stopped_by_pause,
        paused_by_id=requested.user_id,
        paused_by_label=requested.label,
        paused_via=requested.via,
        pause_message=requested.message,
        pause_message_state=PauseMessageState.PENDING,
    )
    clear_pause_request(pivot)
    return help_request


def pause_pending_for(pivot: TestRunTestCase) -> bool:
    """The loaded case is running with a pause request (no query).

    For socket payloads and lists; the generation is not compared here (a
    request of an older generation is cleared at the next pick-up).
    """
    return (
        pivot.status == TestRunTestCaseStatus.IN_PROGRESS
        and pivot.pause_requested_generation is not None
    )
