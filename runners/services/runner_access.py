"""Who may use, manage and see a Test Runner: the one predicate set.

- ``usable_runners``: the runners a user may pick for a project (their own,
  non-archived, while they are a writable member of it).
- ``can_assign``: may assign the runner to a run of the project (Q2: only the
  owner; Q8: a superuser's flag does not extend to other users' runners).
- ``can_manage``: owner-only actions (rename, token, download, update,
  release, archive).
- ``can_force_disconnect``: the owner or an active superuser (Q8).
- ``can_view``: the owner, an active superuser, or a member of a project whose
  active run holds the runner (run card, status socket).
- ``runner_serves_project``: a runner keeps serving a project (a run's
  Resume / Continue by any member, a main chat's pick) only while it is not
  archived and its owner is an active writable member of that project.
- ``load_usable_runners``: the rows behind every runner picker
  (``USABLE_RUNNER_FIELDS``).
"""

from __future__ import annotations

from typing import Final

from django.db.models import QuerySet

from accounts.models import CustomUser
from projects.models import (
    ACTIVE_RUN_STATUSES,
    WRITABLE_ROLES,
    Project,
    ProjectMembership,
    TestRun,
)
from projects.project_access import has_superuser_project_control
from runners.models import TestRunner

# The columns every runner picker loads: the choice label and the badge state
# (``runners.machine_state.runner_machine_state``), never the token.
USABLE_RUNNER_FIELDS: Final = (
    "id",
    "owner_id",
    "name",
    "archived_at",
    "connected",
    "system_info",
    "capabilities",
    "device_state",
    "device_reported_at",
)


def _is_writable_member(user: CustomUser, project_id: int) -> bool:
    return ProjectMembership.objects.filter(
        project_id=project_id, user_id=user.pk, role__in=WRITABLE_ROLES
    ).exists()


def owned_runners(user: CustomUser) -> QuerySet[TestRunner]:
    """The user's non-archived runners, by name (no membership check)."""
    return TestRunner.objects.filter(
        owner_id=user.pk, archived_at__isnull=True
    ).order_by("name", "pk")


def usable_runners(user: CustomUser, project: Project) -> QuerySet[TestRunner]:
    """The runners the user may pick for the project (two queries at most).

    Empty unless the user is active and a writable member of the project: a
    superuser's implicit project access does not make the project's data go
    to their machines through the runner window, which lists memberships only.
    """
    if not user.is_active or project.archived:
        return TestRunner.objects.none()
    if not _is_writable_member(user, project.pk):
        return TestRunner.objects.none()
    return owned_runners(user)


def load_usable_runners(user: CustomUser, project: Project) -> list[TestRunner]:
    """``usable_runners`` with ``USABLE_RUNNER_FIELDS`` (two queries at most)."""
    return list(usable_runners(user, project).only(*USABLE_RUNNER_FIELDS))


def runner_serves_project(runner: TestRunner, project_id: int) -> bool:
    """Whether the runner may still serve ``project_id``: not archived and its
    owner an active writable member of the project (one query, none for an
    archived runner). A superuser's implicit project access does not count."""
    if runner.archived_at is not None:
        return False
    return ProjectMembership.objects.filter(
        project_id=project_id,
        user_id=runner.owner_id,
        user__is_active=True,
        role__in=WRITABLE_ROLES,
    ).exists()


def runner_serves_project_id(runner_id: int, project_id: int) -> bool:
    """``runner_serves_project`` from ids alone, in one ``exists()`` query
    (a missing runner does not serve)."""
    return ProjectMembership.objects.filter(
        project_id=project_id,
        role__in=WRITABLE_ROLES,
        user__is_active=True,
        user__test_runners__id=runner_id,
        user__test_runners__archived_at__isnull=True,
    ).exists()


def can_assign(user: CustomUser, runner: TestRunner, project: Project) -> bool:
    """Whether ``user`` may assign ``runner`` to a run of ``project``."""
    return (
        user.is_active
        and runner.owner_id == user.pk
        and runner.archived_at is None
        and not project.archived
        and _is_writable_member(user, project.pk)
    )


def can_manage(user: CustomUser, runner: TestRunner) -> bool:
    """Owner-only actions; never a superuser on someone else's runner."""
    return user.is_active and runner.owner_id == user.pk


def can_force_disconnect(user: CustomUser, runner: TestRunner) -> bool:
    return can_manage(user, runner) or has_superuser_project_control(user)


def can_view(user: CustomUser, runner: TestRunner) -> bool:
    """The owner, an active superuser, or a member of a project whose active
    run holds the runner (one query in the last case)."""
    return can_view_by_ids(user, runner_id=runner.pk, owner_id=runner.owner_id)


def can_view_by_ids(user: CustomUser, *, runner_id: int, owner_id: int) -> bool:
    """``can_view`` from the runner's ids alone (no runner row needed)."""
    if not user.is_active:
        return False
    if owner_id == user.pk or has_superuser_project_control(user):
        return True
    return TestRun.objects.filter(
        runner_id=runner_id,
        status__in=ACTIVE_RUN_STATUSES,
        project__archived=False,
        project__memberships__user_id=user.pk,
    ).exists()
