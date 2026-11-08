from __future__ import annotations

from typing import Any, cast

from django import forms
from django.db import models
from django.http import QueryDict

from mcp_gateway.limits import MCP_OAUTH_GRANT_MAX_PROJECTS
from mcp_gateway.services.oauth_access import WritableProject

_TOO_MANY_PROJECTS = (
    f"Choose at most {MCP_OAUTH_GRANT_MAX_PROJECTS} projects for one connection."
)


class ConsentDecision(models.TextChoices):
    ALLOW = "allow", "Allow"
    DENY = "deny", "Deny"


def _projects_field(*, required: bool) -> forms.TypedMultipleChoiceField:
    """Checkboxes over the user's writable projects (choices set per form)."""
    return forms.TypedMultipleChoiceField(
        coerce=int,
        required=required,
        widget=forms.CheckboxSelectMultiple,
        label="Projects",
        error_messages={
            "invalid_choice": "Choose only projects you can edit.",
            "required": "Choose at least one project.",
        },
    )


def _set_project_choices(
    form: forms.Form, projects: list[WritableProject]
) -> forms.TypedMultipleChoiceField:
    # django-stubs types `self.fields[...]` as the base `Field`; this is the
    # TypedMultipleChoiceField built by `_projects_field`.
    field = cast(forms.TypedMultipleChoiceField, form.fields["projects"])
    field.choices = [(p.id, p.name) for p in projects]
    return field


def _check_project_count(form: forms.Form, cleaned: dict[str, Any]) -> None:
    chosen = cleaned.get("projects") or []
    if len(chosen) > MCP_OAUTH_GRANT_MAX_PROJECTS:
        form.add_error("projects", _TOO_MANY_PROJECTS)


class ConsentForm(forms.Form):
    """The consent page: which projects to connect, and Allow or Deny.

    At least one project is required only for Allow; Deny needs nothing else.
    """

    projects = _projects_field(required=False)
    decision = forms.ChoiceField(choices=ConsentDecision.choices)

    def __init__(
        self,
        projects: list[WritableProject],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        field = _set_project_choices(self, projects)
        if len(projects) == 1:
            field.initial = [projects[0].id]

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("decision") == ConsentDecision.DENY:
            # Deny never needs a project: a stale or tampered choice must not
            # stop someone from refusing.
            self.errors.pop("projects", None)
            cleaned["projects"] = []
            return cleaned
        if (
            cleaned.get("decision") == ConsentDecision.ALLOW
            and "projects" not in self.errors
        ):
            if not cleaned.get("projects"):
                self.add_error("projects", "Choose at least one project to connect.")
            else:
                _check_project_count(self, cleaned)
        return cleaned


class GrantProjectsForm(forms.Form):
    """The Edit projects page: which projects a live connection covers."""

    projects = _projects_field(required=True)

    def __init__(
        self,
        projects: list[WritableProject],
        data: QueryDict | dict[str, Any] | None = None,
        *,
        selected_ids: list[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        field = _set_project_choices(self, projects)
        field.initial = list(selected_ids or [])

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if "projects" not in self.errors:
            _check_project_count(self, cleaned)
        return cleaned
