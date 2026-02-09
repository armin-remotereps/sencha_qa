from __future__ import annotations

from django.contrib.auth import authenticate, login
from django.http import HttpRequest

from accounts.models import CustomUser


def authenticate_user(
    request: HttpRequest, email: str, password: str
) -> CustomUser | None:
    user = authenticate(request, email=email, password=password)
    if not isinstance(user, CustomUser):
        return None
    return user


def login_user(request: HttpRequest, user: CustomUser) -> None:
    login(request, user)
