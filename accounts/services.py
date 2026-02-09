from __future__ import annotations

from django.contrib.auth import authenticate, login
from django.http import HttpRequest

from accounts.models import CustomUser


def authenticate_user(
    request: HttpRequest, email: str, password: str
) -> CustomUser | None:
    try:
        user = CustomUser.objects.get(email=email)
    except CustomUser.DoesNotExist:
        return None
    authenticated = authenticate(request, username=user.username, password=password)
    if not isinstance(authenticated, CustomUser):
        return None
    return authenticated


def login_user(request: HttpRequest, user: CustomUser) -> None:
    login(request, user)
