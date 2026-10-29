"""Use case: a superuser sets the project's run mode.

The project's ``default_help_mode`` is the mode of every new run. It can also
be applied to existing runs: drafts (and, for ``all``, finished runs) by bulk
update; running and paused runs one by one through
``run_help_mode_services.change_run_help_mode``, so its person step hand-back,
Night auto-fix start and broadcasts apply unchanged. At most one run per
project is running or paused (``start_test_run`` and the reopen services check
it under the project lock), so the loop over open runs is short.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from accounts.models import CustomUser
from projects.exceptions import HelpModeNotAllowedError, RunHelpError
from projects.models import Project, ProjectRunModeScope, TestRun, TestRunStatus
from projects.run_help_mode_services import change_run_help_mode
from projects.run_reopen_services import is_valid_help_mode

_UNKNOWN_MODE = "Choose Attended, Night or Human in the loop."
_FINISHED_STATUSES = (TestRunStatus.DONE, TestRunStatus.CANCELLED)
_OPEN_STATUSES = (TestRunStatus.STARTED, TestRunStatus.PAUSED)


@dataclass(frozen=True)
class ProjectRunModeChange:
    # False when the project already had that mode.
    changed: bool
    # Drafts whose mode was rewritten.
    drafts_updated: int
    # Finished runs whose mode was rewritten (``all`` only).
    finished_updated: int
    # Running or paused runs switched through ``change_run_help_mode``.
    active_switched: int
    # Running or paused runs that could not be switched (finished meanwhile).
    active_skipped: int
    # Person steps handed back to the agent by those switches.
    handed_back: int


def change_project_run_mode(
    *,
    project: Project,
    user: CustomUser,
    help_mode: str,
    scope: ProjectRunModeScope,
) -> ProjectRunModeChange:
    """Set ``project.default_help_mode`` and apply it to the runs ``scope`` names.

    ``user`` is the writable member making the change (authorized by the
    view). Raises ``RunHelpError`` for an unknown mode and
    ``HelpModeNotAllowedError`` when ``user`` is not a superuser, both before
    any lock.
    """
    if not is_valid_help_mode(help_mode):
        raise RunHelpError(_UNKNOWN_MODE)
    if not user.is_superuser:
        raise HelpModeNotAllowedError()

    changed, drafts_updated, finished_updated = _save_mode_and_bulk_update(
        project_id=project.pk, help_mode=help_mode, scope=scope
    )
    project.default_help_mode = help_mode

    active_switched = active_skipped = handed_back = 0
    if scope in (ProjectRunModeScope.OPEN, ProjectRunModeScope.ALL):
        runs = (
            TestRun.objects.filter(project_id=project.pk, status__in=_OPEN_STATUSES)
            .exclude(help_mode=help_mode)
            .only("id")
            .order_by("pk")
        )
        for run in runs:
            # Each switch has its own transaction and the project-then-run
            # lock order, and reuses the hand-back, Night auto-fix start and
            # broadcasts of a single run's switch.
            try:
                result = change_run_help_mode(
                    test_run=run, user=user, help_mode=help_mode
                )
            except RunHelpError:
                # The run finished since it was listed.
                active_skipped += 1
                continue
            active_switched += 1
            if result.handed_back:
                handed_back += 1

    return ProjectRunModeChange(
        changed=changed,
        drafts_updated=drafts_updated,
        finished_updated=finished_updated,
        active_switched=active_switched,
        active_skipped=active_skipped,
        handed_back=handed_back,
    )


@transaction.atomic
def _save_mode_and_bulk_update(
    *, project_id: int, help_mode: str, scope: ProjectRunModeScope
) -> tuple[bool, int, int]:
    """Lock the project, save its mode, rewrite drafts (and finished runs).

    Returns ``(changed, drafts_updated, finished_updated)``. These runs have no
    live person step, help request or worker, so a bulk write is safe; a draft
    started meanwhile no longer matches ``status`` once Postgres re-checks the
    row after waiting on its lock, so it is left to ``start_test_run``.
    """
    locked = (
        Project.objects.select_for_update()
        .only("id", "default_help_mode")
        .get(pk=project_id)
    )
    changed = locked.default_help_mode != help_mode
    if changed:
        locked.default_help_mode = help_mode
        locked.save(update_fields=["default_help_mode", "updated_at"])
    if scope == ProjectRunModeScope.NEW_ONLY:
        return changed, 0, 0

    now = timezone.now()
    runs = TestRun.objects.filter(project_id=project_id).exclude(help_mode=help_mode)
    drafts_updated = runs.filter(status=TestRunStatus.WAITING).update(
        help_mode=help_mode, updated_at=now
    )
    finished_updated = 0
    if scope == ProjectRunModeScope.ALL:
        finished_updated = runs.filter(status__in=_FINISHED_STATUSES).update(
            help_mode=help_mode, updated_at=now
        )
    return changed, drafts_updated, finished_updated
