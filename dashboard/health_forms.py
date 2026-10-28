from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django import forms
from django.http import QueryDict

from dashboard.services.machine_health import (
    DEFAULT_MACHINE_HEALTH_DAYS,
    MACHINE_HEALTH_DAY_CHOICES,
)


class MachineHealthFilterForm(forms.Form):
    """GET filters of the Machine health page. ``project`` choices are the
    user's member projects (passed in by the view), so they authorize it."""

    project = forms.TypedChoiceField(
        required=False,
        coerce=int,
        empty_value=None,
        label="Project",
        widget=forms.Select(attrs={"class": "select"}),
    )
    days = forms.TypedChoiceField(
        required=False,
        coerce=int,
        initial=DEFAULT_MACHINE_HEALTH_DAYS,
        choices=[(days, f"Last {days} days") for days in MACHINE_HEALTH_DAY_CHOICES],
        label="Period",
        widget=forms.Select(attrs={"class": "select"}),
    )

    def __init__(
        self,
        project_choices: Sequence[tuple[int, str]],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        project_field = self.fields["project"]
        assert isinstance(project_field, forms.TypedChoiceField)
        project_field.choices = [("", "All projects"), *project_choices]

    def filters(self) -> tuple[int | None, int]:
        """``(project_id, days)``: the valid values, defaults for invalid ones."""
        # An invalid bound form keeps only its valid fields in ``cleaned_data``;
        # an unbound form has none.
        self.is_valid()
        cleaned: dict[str, Any] = getattr(self, "cleaned_data", {})
        project_id = cleaned.get("project")
        days = cleaned.get("days")
        return (
            project_id if isinstance(project_id, int) else None,
            days if isinstance(days, int) else DEFAULT_MACHINE_HEALTH_DAYS,
        )
