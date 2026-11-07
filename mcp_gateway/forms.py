from __future__ import annotations

from typing import Any, cast

from django import forms
from django.db import models
from django.http import QueryDict

from mcp_gateway.services.oauth_access import WritableProject


class ConsentDecision(models.TextChoices):
    ALLOW = "allow", "Allow"
    DENY = "deny", "Deny"


class ConsentForm(forms.Form):
    """The consent page: which project to connect, and Allow or Deny.

    The project is required only for Allow; Deny needs nothing else.
    """

    project = forms.TypedChoiceField(
        coerce=int,
        empty_value=None,
        required=False,
        widget=forms.RadioSelect,
        label="Project",
        error_messages={"invalid_choice": "Choose a project you can edit."},
    )
    decision = forms.ChoiceField(choices=ConsentDecision.choices)

    def __init__(
        self,
        projects: list[WritableProject],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        # django-stubs types `self.fields[...]` as the base `Field`; this is
        # the TypedChoiceField declared above.
        project_field = cast(forms.TypedChoiceField, self.fields["project"])
        project_field.choices = [(p.id, p.name) for p in projects]
        if len(projects) == 1:
            project_field.initial = projects[0].id

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("decision") == ConsentDecision.DENY:
            # Deny never needs a project: a stale or tampered choice must not
            # stop someone from refusing.
            self.errors.pop("project", None)
            cleaned["project"] = None
            return cleaned
        if (
            cleaned.get("decision") == ConsentDecision.ALLOW
            and cleaned.get("project") is None
            and "project" not in self.errors
        ):
            self.add_error("project", "Choose a project to connect.")
        return cleaned
