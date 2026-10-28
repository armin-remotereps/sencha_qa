"""The run page's view of a paused (or person-confirming) run: which banner
shows, with what gates.

The reopen gates (``list_reopen_blockers``) run once, only while the run is
PAUSED, and are shared by the person step panel, the help panel and the plain
"Continue run" banner. The help panel is not loaded while a person step holds
the run: the banner shows the person step then, and an earlier, resolved help
request of the run would cost its own queries for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from projects.case_help_services import HelpPanel, get_help_panel
from projects.case_help_state import RunHelpState, get_run_help_state
from projects.models import Project, TestRun, TestRunStatus
from projects.person_step_services import PersonStepPanel, get_person_step_panel
from projects.run_reopen_services import list_reopen_blockers


@dataclass(frozen=True)
class RunPausePanels:
    # The latest help request's panel; None unless the run is PAUSED, has a
    # help request and no person step holds it.
    help_panel: HelpPanel | None
    # The run's open person step (Human in the loop); None otherwise.
    person_step_panel: PersonStepPanel | None
    # Gates of a PAUSED run with neither panel (a person step was marked
    # failed or handed back and a gate refused the queueing): the plain
    # "Continue run" banner. Empty otherwise.
    paused_run_blockers: tuple[str, ...]
    help_state: RunHelpState


def get_run_pause_panels(test_run: TestRun, project: Project) -> RunPausePanels:
    """The run page's help / person step banners; no queries unless active.

    PAUSED on a person step: the step (1 query) and the gates. Otherwise the
    help panel as before, sharing the same gates.
    """
    paused = test_run.status == TestRunStatus.PAUSED
    blockers = list_reopen_blockers(test_run, project) if paused else None
    person_step_panel = get_person_step_panel(test_run, project, blockers=blockers)
    holds_run = person_step_panel is not None and person_step_panel.holds_run
    help_panel = (
        None if holds_run else get_help_panel(test_run, project, blockers=blockers)
    )
    paused_run_blockers = (
        tuple(blockers)
        if blockers is not None and help_panel is None and person_step_panel is None
        else ()
    )
    # On a PAUSED run without a person step holding it, ``get_help_panel``
    # loaded the run's latest request (None: the run has none).
    help_state = get_run_help_state(
        test_run,
        person_step_open=holds_run,
        latest_auto_fix_state=(
            help_panel.request.auto_fix_state if help_panel is not None else ""
        ),
    )
    return RunPausePanels(
        help_panel=help_panel,
        person_step_panel=person_step_panel,
        paused_run_blockers=paused_run_blockers,
        help_state=help_state,
    )
