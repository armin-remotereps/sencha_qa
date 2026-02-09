from django.contrib.auth.models import AbstractUser

from accounts.managers import CustomUserManager


class CustomUser(AbstractUser):
    objects: CustomUserManager = CustomUserManager()  # type: ignore[misc]
