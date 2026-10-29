"""Superuser-only Users page: list users and create a regular user."""

from __future__ import annotations

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.decorators import superuser_required
from accounts.exceptions import UserEmailTakenError
from accounts.forms import UserCreateForm
from accounts.types import AuthenticatedRequest
from accounts.user_management_services import create_user, list_users
from auto_tester.pagination import (
    ALLOWED_PER_PAGE,
    elided_page_range,
    parse_page,
    parse_per_page,
)


def _render_users(request: AuthenticatedRequest, form: UserCreateForm) -> HttpResponse:
    per_page = parse_per_page(request)
    users = list_users(page=parse_page(request), per_page=per_page)
    return render(
        request,
        "accounts/users.html",
        {
            "active_nav": "users",
            "users": users,
            "form": form,
            "page_obj": users,
            "elided_page_range": elided_page_range(users),
            "per_page": per_page,
            "allowed_per_page": ALLOWED_PER_PAGE,
            "query_params": f"&per_page={per_page}",
        },
    )


@superuser_required
@require_GET
def users(request: AuthenticatedRequest) -> HttpResponse:
    return _render_users(request, UserCreateForm())


@superuser_required
@require_POST
def user_create(request: AuthenticatedRequest) -> HttpResponse:
    form = UserCreateForm(request.POST)
    if not form.is_valid():
        return _render_users(request, form)
    try:
        user = create_user(
            actor=request.user,
            email=form.cleaned_data["email"],
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            password=form.cleaned_data["password1"],
        )
    except UserEmailTakenError as exc:
        form.add_error("email", exc.message)
        return _render_users(request, form)
    messages.success(request, f"Created {user.email}.")
    return redirect("accounts:users")
