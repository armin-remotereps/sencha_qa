"""Project membership: list members, invite one by email, change a role, leave."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.cache import cache
from django.db import transaction

from accounts.models import CustomUser
from projects.exceptions import (
    AlreadyMemberError,
    InvalidMemberRoleError,
    LastProjectMemberError,
    LastWritableMemberError,
    MemberInviteThrottledError,
    MemberNotFoundError,
    MemberRoleChangeNotAllowedError,
    NotProjectMemberError,
)
from projects.models import Project, ProjectMembership, ProjectRole

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
    role: str
    role_label: str
    can_write: bool


@dataclass(frozen=True)
class ProjectMembers:
    rows: list[ProjectMemberRow]
    # Another active member remains, and, for a writable viewer, another
    # active writable one (a read-only viewer never holds the invariant).
    can_leave: bool
    viewer_can_write: bool


def list_project_members(project: Project, *, viewer: CustomUser) -> ProjectMembers:
    """All members of ``project`` with their roles, ordered by email, in one query."""
    memberships = (
        ProjectMembership.objects.filter(project=project)
        .select_related("user")
        .only(
            "role",
            "user__id",
            "user__email",
            "user__first_name",
            "user__last_name",
            "user__is_active",
        )
        .order_by("user__email")
    )
    rows = [
        ProjectMemberRow(
            user_id=membership.user.pk,
            email=membership.user.email,
            full_name=membership.user.get_full_name(),
            is_you=membership.user.pk == viewer.pk,
            is_active=membership.user.is_active,
            role=membership.role,
            role_label=ProjectRole(membership.role).label,
            can_write=membership.can_write,
        )
        for membership in memberships
    ]
    viewer_can_write = any(row.is_you and row.can_write for row in rows)
    others = [row for row in rows if row.is_active and not row.is_you]
    can_leave = bool(others) and (
        not viewer_can_write or any(row.can_write for row in others)
    )
    return ProjectMembers(
        rows=rows, can_leave=can_leave, viewer_can_write=viewer_can_write
    )


def _validate_role(role: str) -> None:
    if role not in ProjectRole.values:
        raise InvalidMemberRoleError()


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


def _has_other_active_writer(project: Project, *, user_id: int) -> bool:
    return (
        ProjectMembership.objects.filter(
            project=project, role=ProjectRole.MEMBER, user__is_active=True
        )
        .exclude(user_id=user_id)
        .exists()
    )


def invite_member(
    *,
    project: Project,
    inviter: CustomUser,
    email: str,
    role: str = ProjectRole.MEMBER,
) -> CustomUser:
    """Add the active user with ``email`` to ``project`` at once, with ``role``.

    Raises ``InvalidMemberRoleError``, ``MemberInviteThrottledError``,
    ``MemberNotFoundError`` or ``AlreadyMemberError``.
    """
    _validate_role(role)
    _check_invite_throttle(inviter)
    user = _find_active_user(email)
    with transaction.atomic():
        _lock_project(project)
        if project.members.filter(pk=user.pk).exists():
            raise AlreadyMemberError()
        project.members.add(user, through_defaults={"role": role})
    return user


def change_member_role(
    *, project: Project, actor: CustomUser, user_id: int, role: str
) -> None:
    """Set the role of member ``user_id``; ``actor`` must be a writable member.

    The project always keeps an active writable member, so demoting the last
    one (yourself included) is refused. The same role is a no-op.

    Raises ``InvalidMemberRoleError``, ``NotProjectMemberError`` (the target
    is no longer a member), ``LastWritableMemberError``, or ``MemberRoleChangeNotAllowedError``
    when ``actor`` is not a writable member (the view's gate normally stops it).
    """
    _validate_role(role)
    with transaction.atomic():
        # Same lock as invite and leave, so two demotions cannot both pass the
        # "another writer remains" check.
        _lock_project(project)
        if not ProjectMembership.objects.filter(
            project=project, user=actor, role=ProjectRole.MEMBER
        ).exists():
            raise MemberRoleChangeNotAllowedError()
        membership = (
            ProjectMembership.objects.filter(project=project, user_id=user_id)
            .only("id", "role")
            .first()
        )
        if membership is None:
            raise NotProjectMemberError()
        if membership.role == role:
            return
        if membership.role == ProjectRole.MEMBER and not _has_other_active_writer(
            project, user_id=user_id
        ):
            raise LastWritableMemberError()
        membership.role = role
        membership.save(update_fields=["role"])


def leave_project(*, project: Project, user: CustomUser) -> None:
    """Remove ``user`` from ``project`` if the project keeps its members.

    Another active member must remain; a writable member also needs another
    active writable member to remain. A read-only member never holds that
    invariant.

    Raises ``NotProjectMemberError``, ``LastProjectMemberError`` or
    ``LastWritableMemberError``.
    """
    with transaction.atomic():
        # The lock makes two last members leaving at once count each other's leave.
        _lock_project(project)
        membership = (
            ProjectMembership.objects.filter(project=project, user=user)
            .only("id", "role")
            .first()
        )
        if membership is None:
            raise NotProjectMemberError()
        others = project.members.filter(is_active=True).exclude(pk=user.pk)
        if not others.exists():
            raise LastProjectMemberError()
        if membership.can_write and not _has_other_active_writer(
            project, user_id=user.pk
        ):
            raise LastWritableMemberError()
        project.members.remove(user)
