from functools import partial

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.db import transaction
from django.forms import ModelForm
from django.http import HttpRequest

from accounts.models import CustomUser
from runners.services.disconnect_owner_runners import disconnect_owner_runners


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin[CustomUser]):
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("email", "first_name", "last_name")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "first_name",
                    "last_name",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def save_model(
        self,
        request: HttpRequest,
        obj: CustomUser,
        form: ModelForm[CustomUser],
        change: bool,
    ) -> None:
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data and not obj.is_active:
            # A deactivated owner's Test Runners stop serving: their live
            # sockets close once the change is committed.
            transaction.on_commit(partial(disconnect_owner_runners, obj.pk))
