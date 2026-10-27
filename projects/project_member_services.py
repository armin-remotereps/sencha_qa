"""Project membership: list members, invite one by email, leave."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache
from django.db import transaction

from accounts.models import CustomUser
from projects.exceptions import (
    AlreadyMemberError,
    LastProjectMemberError,
    MemberInviteThrottledError,
    MemberNotFoundError,
    NotProjectMemberError,
)
from projects.models import Project

# Invite attempts per inviter per window; limits probing which emails exist.
INVITE_LIMIT = 10
INVITE_WINDOW_SECONDS = 600


@dataclass(frozen=True)
class ProjectMemberRow:
    user_id: int
    email: str
    full_name: str
    is_you: bool
    is_active: bool


@dataclass(frozen=True)
class ProjectMembers:
    rows: list[ProjectMemberRow]
    # True when at least one other active member would remain after leaving.
    can_leave: bool


def list_project_members(project: Project, *, viewer: CustomUser) -> ProjectMembers:
    """All members of ``project``, ordered by email, in one query."""
    users = project.members.only(
        "id", "email", "first_name", "last_name", "is_active"
    ).order_by("email")
    rows = [
        ProjectMemberRow(
            user_id=user.pk,
            email=user.email,
            full_name=user.get_full_name(),
            is_you=user.pk == viewer.pk,
            is_active=user.is_active,
        )
        for user in users
    ]
    can_leave = any(row.is_active and not row.is_you for row in rows)
    return ProjectMembers(rows=rows, can_leave=can_leave)


def _throttle_key(user_id: int) -> str:
    return f"projects:member-invite:{user_id}"


def _check_invite_throttle(inviter: CustomUser) -> None:
    key = _throttle_key(inviter.pk)
    cache.add(key, 0, timeout=INVITE_WINDOW_SECONDS)
    try:
        attempts = cache.incr(key)
    except ValueError:
        # The key expired between add and incr: this is the window's first attempt.
        cache.add(key, 1, timeout=INVITE_WINDOW_SECONDS)
        attempts = 1
    if attempts > INVITE_LIMIT:
        raise MemberInviteThrottledError(INVITE_WINDOW_SECONDS)


def _find_active_user(email: str) -> CustomUser:
    email = email.strip()
    active = CustomUser.objects.filter(is_active=True).only("id", "email")
    # Emails are unique only by exact case: prefer the exact match, then accept
    # a case-insensitive one only when it is unambiguous.
    exact = active.filter(email=email).first()
    if exact is not None:
        return exact
    matches = list(active.filter(email__iexact=email)[:2])
    if len(matches) == 1:
        return matches[0]
    raise MemberNotFoundError()


def _lock_project(project: Project) -> None:
    """Serialize membership changes of one project (call inside a transaction)."""
    Project.objects.select_for_update().only("id").get(pk=project.pk)


def invite_member(*, project: Project, inviter: CustomUser, email: str) -> CustomUser:
    """Add the active user with ``email`` to ``project`` at once.

    Raises ``MemberInviteThrottledError``, ``MemberNotFoundError`` or
    ``AlreadyMemberError``.
    """
    _check_invite_throttle(inviter)
    user = _find_active_user(email)
    with transaction.atomic():
        _lock_project(project)
        if project.members.filter(pk=user.pk).exists():
            raise AlreadyMemberError()
        project.members.add(user)
    return user


def leave_project(*, project: Project, user: CustomUser) -> None:
    """Remove ``user`` from ``project`` if another active member remains.

    Raises ``NotProjectMemberError`` or ``LastProjectMemberError``.
    """
    with transaction.atomic():
        # The lock makes two last members leaving at once count each other's leave.
        _lock_project(project)
        if not project.members.filter(pk=user.pk).exists():
            raise NotProjectMemberError()
        others = project.members.filter(is_active=True).exclude(pk=user.pk)
        if not others.exists():
            raise LastProjectMemberError()
        project.members.remove(user)
