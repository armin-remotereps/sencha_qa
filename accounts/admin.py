from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import CustomUser


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):  # type: ignore[type-arg]
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "first_name",
                    "last_name",
                    "password1",
                    "password2",
                ),
            },
        ),
    )
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name", "username")
