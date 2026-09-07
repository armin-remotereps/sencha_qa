from __future__ import annotations

from django import forms


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
