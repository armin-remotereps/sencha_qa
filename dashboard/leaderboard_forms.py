from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from django import forms
from django.http import QueryDict

from dashboard.leaderboard_types import (
    DEFAULT_PERIOD,
    LeaderboardFilters,
    LeaderboardPeriod,
)
from projects.models import RunHelpMode


class LeaderboardFilterForm(forms.Form):
    """GET filters of the Leaderboard page. ``project`` choices are the user's
    member projects (passed in by the view), so an unknown or foreign project
    id is not a valid choice and falls back to all projects."""

    period = forms.ChoiceField(
        required=False,
        initial=DEFAULT_PERIOD.value,
        choices=[(period.value, period.label) for period in LeaderboardPeriod],
        widget=forms.HiddenInput,
    )
    project = forms.TypedChoiceField(
        required=False,
        coerce=int,
        empty_value=None,
        label="Project",
        widget=forms.Select(attrs={"class": "select"}),
    )
    mode = forms.ChoiceField(
        required=False,
        choices=[("", "All run modes"), *RunHelpMode.choices],
        label="Run mode",
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

    def filters(self) -> LeaderboardFilters:
        """The valid values, defaults for invalid or missing ones."""
        # An invalid bound form keeps only its valid fields in ``cleaned_data``;
        # an unbound form has none.
        self.is_valid()
        cleaned: dict[str, Any] = getattr(self, "cleaned_data", {})
        period = cleaned.get("period")
        project_id = cleaned.get("project")
        mode = cleaned.get("mode")
        return LeaderboardFilters(
            period=LeaderboardPeriod(period) if period else DEFAULT_PERIOD,
            project_id=project_id if isinstance(project_id, int) else None,
            help_mode=mode if mode else None,
        )
