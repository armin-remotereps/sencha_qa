"""Change a run's help mode while it is a draft, running or paused.

The new mode applies from the next Need help (``services._pause_run_for_help``
reads ``help_mode`` under the run lock). One exception: a paused run whose
open request waits for a person and never had an auto-fix gets one at once
when switched to Night, if the step is eligible. Switching to Attended never
stops a running auto-fix; Stop auto-fix on the case page does that.

Human in the loop applies from the next step (the worker reads the mode at
each step boundary). Choosing any other mode (even the current one) while a
step waits for a person hands that step back to the agent at once
(``hand_back_person_step``). A Need help in a Human in the loop run behaves
as in Attended: the auto-fix paths are gated on Night only.

Imports the leaf ``machine_setup`` modules only (through ``services`` and
``case_help_state``), never
``machine_setup.services`` (that package imports ``projects``).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Exists, OuterRef

from accounts.models import CustomUser
from machine_setup.models import SetupConversation
from projects.case_help_policy import auto_fix_eligible
from projects.case_help_state import enqueue_case_auto_fix, mark_auto_fix_running
from projects.exceptions import HelpModeNotAllowedError, RunHelpError
from projects.models import (
    CaseHelpRequest,
    HelpAutoFixState,
    HelpResolution,
    RunHelpMode,
    TestRun,
    TestRunStatus,
)
from projects.person_step_services import hand_back_person_step
from projects.run_reopen_services import is_valid_help_mode, lock_project_and_run
from projects.services import broadcast_run_and_pivots, broadcast_test_run_status

_CHANGEABLE_STATUSES = (
    TestRunStatus.WAITING,
    TestRunStatus.STARTED,
    TestRunStatus.PAUSED,
)
_NOT_CHANGEABLE = "The help mode can only be changed while the run is active."
_UNKNOWN_MODE = "Choose Attended, Night or Human in the loop."


@dataclass(frozen=True)
class HelpModeChange:
    help_mode: str
    # False when the run already had that mode (nothing was written).
    changed: bool
    # The paused run's waiting case was handed to the auto-fix.
    auto_fix_started: bool
    # A step waiting for a person was handed back to the agent.
    handed_back: bool = False
    # User-safe reason the run stays paused after a hand-back (a gate
    # refused; "Continue run" queues it later); empty otherwise.
    blocker: str = ""


def change_run_help_mode(
    *, test_run: TestRun, user: CustomUser, help_mode: str
) -> HelpModeChange:
    """Save ``help_mode`` on the run; start an auto-fix where Night now applies.

    ``user`` is the member making the change (authorized by the view). Only a
    superuser may change the mode (``HelpModeNotAllowedError``), whatever the
    value, refused before any lock.
    """
    if not is_valid_help_mode(help_mode):
        raise RunHelpError(_UNKNOWN_MODE)
    if not user.is_superuser:
        raise HelpModeNotAllowedError()
    with transaction.atomic():
        _, locked_run = lock_project_and_run(test_run.pk)
        if locked_run.status not in _CHANGEABLE_STATUSES:
            raise RunHelpError(_NOT_CHANGEABLE)
        changed = locked_run.help_mode != help_mode
        if changed:
            locked_run.help_mode = help_mode
            locked_run.save(update_fields=["help_mode", "updated_at"])

        # Outside Human in the loop, a step waiting for a person goes back to
        # the agent (it broadcasts and queues the run itself); a step being
        # confirmed finishes first. Checked before the unchanged-mode return,
        # so choosing the current mode again recovers a run left on a step.
        if help_mode != RunHelpMode.HUMAN_IN_THE_LOOP:
            hand_back = hand_back_person_step(locked_run)
            if hand_back is not None:
                return HelpModeChange(
                    help_mode,
                    changed=changed,
                    auto_fix_started=False,
                    handed_back=True,
                    blocker=hand_back.blocker,
                )
        if not changed:
            return HelpModeChange(help_mode, changed=False, auto_fix_started=False)

        request = _request_waiting_for_person(locked_run, help_mode)
        if request is None:
            transaction.on_commit(lambda: broadcast_test_run_status(locked_run))
            return HelpModeChange(help_mode, changed=True, auto_fix_started=False)

        mark_auto_fix_running(request)
        request.save(update_fields=["auto_fix_state", "auto_fix_started_at"])
        pivot = request.test_run_test_case
        request_id = request.pk

        def _after_commit() -> None:
            # Enqueue first: on a broker failure the request falls back to
            # people, and the broadcast (help_state is read from the database)
            # then carries that state.
            enqueue_case_auto_fix(request_id)
            broadcast_run_and_pivots(locked_run, [pivot])

        transaction.on_commit(_after_commit)
        return HelpModeChange(help_mode, changed=True, auto_fix_started=True)


def _request_waiting_for_person(
    test_run: TestRun, help_mode: str
) -> CaseHelpRequest | None:
    """The paused run's open request a Night switch hands to the auto-fix.

    Only one that never had an auto-fix (``auto_fix_state`` none), has no
    thread (a person's help chat is never taken over), no pending person (a
    person paused the case, or asked to: they discuss it themselves) and whose
    step is still eligible (Q10); a not-fixed or running auto-fix is left as
    it is.
    """
    if help_mode != RunHelpMode.NIGHT or test_run.status != TestRunStatus.PAUSED:
        return None
    request = (
        CaseHelpRequest.objects.select_for_update(of=("self",))
        .select_related("test_run_test_case")
        .defer("test_run_test_case__logs")
        .filter(
            test_run_test_case__test_run_id=test_run.pk,
            resolution=HelpResolution.OPEN,
            auto_fix_state=HelpAutoFixState.NONE,
            # No pending person (``has_pending_person``).
            paused_by_label="",
        )
        .exclude(
            Exists(SetupConversation.objects.filter(help_request_id=OuterRef("pk")))
        )
        .first()
    )
    if request is None:
        return None
    if not auto_fix_eligible(request.test_run_test_case_id, request.step_num):
        return None
    return request
