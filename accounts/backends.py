from __future__ import annotations

from typing import Any

from django.contrib.auth.backends import ModelBackend
from django.http import HttpRequest

from accounts.models import CustomUser


class EmailBackend(ModelBackend):
    def authenticate(
        self,
        request: HttpRequest | None,
        username: str | None = None,
        password: str | None = None,
        email: str | None = None,
        **kwargs: Any,
    ) -> CustomUser | None:
        lookup = email or username
        if lookup is None or password is None:
            return None
        try:
            user = CustomUser.objects.get(email=lookup)
        except CustomUser.DoesNotExist:
            CustomUser().set_password(password)
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
