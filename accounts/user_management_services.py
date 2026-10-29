"""Superuser user management: list users and create a regular user.

A sibling of the legacy ``accounts/services.py`` (a ``services/`` package
would shadow that module). Users are created with an initial password set by
the superuser; staff and superuser accounts are not created here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.core.paginator import Page, Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count

from accounts.exceptions import SuperuserRequiredError, UserEmailTakenError
from accounts.models import CustomUser


@dataclass(frozen=True)
class UserRow:
    id: int
    email: str
    full_name: str
    is_superuser: bool
    is_active: bool
    date_joined: datetime
    last_login: datetime | None
    project_count: int


def list_users(*, page: int, per_page: int) -> Page[UserRow]:
    """One page of users ordered by email, with their project membership count."""
    qs = (
        CustomUser.objects.only(
            "id",
            "email",
            "first_name",
            "last_name",
            "is_superuser",
            "is_active",
            "date_joined",
            "last_login",
        )
        .annotate(project_count=Count("project_memberships"))
        .order_by("email")
    )
    paginator: Paginator[CustomUser] = Paginator(qs, per_page)
    user_page = paginator.get_page(page)
    rows = [
        UserRow(
            id=user.id,
            email=user.email,
            full_name=user.get_full_name(),
            is_superuser=user.is_superuser,
            is_active=user.is_active,
            date_joined=user.date_joined,
            last_login=user.last_login,
            project_count=user.project_count,
        )
        for user in user_page.object_list
    ]
    # The paginator counted the users; this page holds the same rows mapped to
    # UserRow, which django-stubs' Page[T] cannot express.
    return Page(rows, user_page.number, paginator)  # type: ignore[arg-type]


def create_user(
    *,
    actor: CustomUser,
    email: str,
    first_name: str,
    last_name: str,
    password: str,
) -> CustomUser:
    """Create an active regular user (not staff, not superuser).

    Raises ``SuperuserRequiredError`` unless ``actor`` is an active superuser and
    ``UserEmailTakenError`` when the email exists in any letter case.
    The password is never logged.
    """
    if not (actor.is_active and actor.is_superuser):
        raise SuperuserRequiredError()
    normalized = CustomUser.objects.normalize_email(email.strip())
    if CustomUser.objects.filter(email__iexact=normalized).exists():
        raise UserEmailTakenError()
    try:
        with transaction.atomic():
            return CustomUser.objects.create_user(
                normalized,
                password,
                first_name=first_name.strip(),
                last_name=last_name.strip(),
                is_staff=False,
                is_superuser=False,
                is_active=True,
            )
    except IntegrityError as exc:
        # Lost a race with another create of the same email.
        raise UserEmailTakenError() from exc
