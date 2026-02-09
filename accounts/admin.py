from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):  # type: ignore[type-arg]
    pass
