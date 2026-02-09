from django import forms

FIELD_CSS = (
    "flex h-10 w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 "
    "text-sm text-zinc-100 placeholder:text-zinc-400 "
    "focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2 "
    "focus:ring-offset-zinc-950"
)


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "you@example.com",
                "autocomplete": "email",
            }
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "Password",
                "autocomplete": "current-password",
            }
        ),
    )
