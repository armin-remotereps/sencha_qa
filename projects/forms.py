from __future__ import annotations

from django import forms

from accounts.forms import FIELD_CSS


class ProjectForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "Project name",
            }
        ),
    )
    tags = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "e.g. python, django, api",
            }
        ),
    )

    def clean_tags(self) -> list[str]:
        raw: str = self.cleaned_data.get("tags", "")
        if not raw.strip():
            return []
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
