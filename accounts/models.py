from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.db import models

from accounts.managers import CustomUserManager


class CustomUser(AbstractUser):
    username = None  # type: ignore[assignment]
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects: CustomUserManager = CustomUserManager()  # type: ignore[assignment,misc]

    def __str__(self) -> str:
        return self.email
