from __future__ import annotations

from typing import Any

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from accounts.models import CustomUser


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "input",
                "placeholder": "you@example.com",
                "autocomplete": "email",
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": "input",
                "placeholder": "Password",
                "autocomplete": "current-password",
            }
        ),
    )


class UserCreateForm(forms.Form):
    """A superuser creates a regular user with an initial password."""

    email = forms.EmailField(
        max_length=254,
        widget=forms.EmailInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    first_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    last_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    password1 = forms.CharField(
        label="Initial password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "autocomplete": "new-password"}
        ),
        help_text="Share it with the user privately; they sign in with it.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "autocomplete": "new-password"}
        ),
    )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2:
            if password1 != password2:
                self.add_error("password2", "The two passwords do not match.")
            else:
                # Transient user so the similarity validator can compare.
                candidate = CustomUser(
                    email=cleaned.get("email") or "",
                    first_name=cleaned.get("first_name") or "",
                    last_name=cleaned.get("last_name") or "",
                )
                try:
                    validate_password(password2, user=candidate)
                except ValidationError as exc:
                    self.add_error("password2", exc)
        return cleaned
