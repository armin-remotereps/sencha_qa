from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import UserManager

if TYPE_CHECKING:
    from accounts.models import CustomUser


class CustomUserManager(UserManager["CustomUser"]):
    pass
