"""Use cases of a person step in a Human in the loop run: report, overrule,
mark failed, mark blocked, and the hand-back when the run leaves the mode.

Imports ``run_reopen_services`` and ``services``, never ``machine_setup.services``.

Every action locks the project, then the run, then the person step, then the
case (the order every help writer uses), requires the run to be PAUSED and the
step to be open with the id the card was rendered for (stale-card guard). The
remaining cases are then queued (the reported case first) through
``run_reopen_services.queue_pending_cases`` inside a savepoint: a gate refusal
keeps the report and leaves the run paused with a "Continue run" fallback.

The agent's confirmation is written by the worker (``projects.services``), not
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import CustomUser
from projects.models import CasePersonStep, Project, TestRun, TestRunTestCase


@dataclass(frozen=True)
class PersonStepPanel:
    """The person step of a paused run, for its case card and run banner."""

    # The open (or latest) step, with ``test_run_test_case__test_case`` loaded.
    step: CasePersonStep
    # "Step N of M".
    step_label: str
    # The step waits for the person (``waiting`` or ``not_confirmed``).
    is_open: bool
    # The agent is confirming the person's report (``reported``).
    confirming: bool
    # The agent's reason while ``not_confirmed``; empty otherwise.
    objection: str
    # Where to run a command step, e.g. "Terminal (bash)"; empty otherwise.
    shell_label: str
    # The person can report now (open and no gate refuses).
    can_report: bool
    # Why the run cannot be queued right now (runner offline, ...).
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PersonStepActionResult:
    # The pending cases are queued: the run continues now.
    queued: bool
    # User-safe reason when a gate refused and the run stays paused.
    blocker: str = ""


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def get_person_step_panel(
    test_run: TestRun, project: Project, *, pivot_id: int | None = None
) -> PersonStepPanel | None:
    """The person step panel of a PAUSED run; None (no queries) otherwise.

    With ``pivot_id``, None unless the run's latest person step is that case's.
    """
    raise NotImplementedError


# ---------------------------------------------------------------------------
# A person's actions
# ---------------------------------------------------------------------------


def report_person_step(
    *,
    pivot: TestRunTestCase,
    user: CustomUser,
    person_step_id: int,
    outcome: str,
    note: str,
    output: str,
) -> PersonStepActionResult:
    """The person performed the step and reports how it went; the agent confirms."""
    raise NotImplementedError


def overrule_person_step(
    *, pivot: TestRunTestCase, user: CustomUser, person_step_id: int, note: str
) -> PersonStepActionResult:
    """The person overrules the agent's objection; the step is recorded passed."""
    raise NotImplementedError


def mark_failed_person_step(
    *, pivot: TestRunTestCase, user: CustomUser, person_step_id: int, note: str
) -> PersonStepActionResult:
    """The person judges the case Failed; the rest of the run continues."""
    raise NotImplementedError


def mark_blocked_person_step(
    *, pivot: TestRunTestCase, user: CustomUser, person_step_id: int, note: str
) -> PersonStepActionResult:
    """The person marks the case Blocked (``help_skipped``); the run continues."""
    raise NotImplementedError


def hand_back_person_step(test_run: TestRun) -> bool:
    """The run left Human in the loop: the agent runs the open step itself.

    Called by the mode switch inside its transaction, with the run locked.
    Only a ``waiting`` or ``not_confirmed`` step is handed back (from the
    current screen, no cleanup, no restore); a ``reported`` one finishes first.
    Returns whether a step was handed back.
    """
    raise NotImplementedError
