"""Forms of the Failures page: filters (GET), bulk actions and presets.

The forms never query: the view passes the user's member project choices
and the selected project's run choices, so the choices double as
authorization.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from django import forms
from django.http import QueryDict

from dashboard.models import PRESET_NAME_MAX_LENGTH, PRESET_QUERY_MAX_LENGTH
from dashboard.services.failed_case_bulk import BULK_MAX_CASES, BulkAction
from dashboard.services.failed_case_filters import (
    FailedByFilter,
    FailedCaseFilters,
    FailedCaseView,
    ReviewFilter,
)

SEARCH_MAX_LENGTH = 200
RETURN_QUERY_MAX_LENGTH = 2000


def _coerce_optional_int(value: str) -> int | None:
    return int(value) if value else None


class FailedCaseFilterForm(forms.Form):
    """GET filters of ``/failures/``; every field optional.

    An invalid field is reported and left out of ``to_filters`` (the rest
    still apply).
    """

    date_from = forms.DateField(
        label="Failed from",
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "input"}),
    )
    date_to = forms.DateField(
        label="Failed until",
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "input"}),
    )
    project = forms.TypedChoiceField(
        label="Project",
        required=False,
        coerce=int,
        empty_value=None,
        choices=(),
        # Run choices belong to one project: a new project clears the run
        # (failureFilters.projectChanged in static/js/punk-hazard.js).
        widget=forms.Select(attrs={"class": "select", "x-on:change": "projectChanged"}),
        error_messages={
            "invalid_choice": "That project is not one of your projects; "
            "showing all projects."
        },
    )
    run = forms.TypedChoiceField(
        label="Run",
        required=False,
        coerce=int,
        empty_value=None,
        choices=(),
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "invalid_choice": "That run has no failed case in the chosen "
            "project; showing all runs."
        },
    )
    q = forms.CharField(
        label="Title or TestRail ID",
        required=False,
        max_length=SEARCH_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={
                "type": "search",
                "class": "input",
                "placeholder": "Title, C123 or 123",
            }
        ),
    )
    review = forms.ChoiceField(
        label="Review",
        required=False,
        choices=[("", "Any review state"), *ReviewFilter.choices],
        widget=forms.Select(attrs={"class": "select"}),
    )
    failed_by = forms.ChoiceField(
        label="Failed by",
        required=False,
        choices=[("", "Anyone"), *FailedByFilter.choices],
        widget=forms.Select(attrs={"class": "select"}),
    )
    view = forms.ChoiceField(
        label="View",
        required=False,
        choices=FailedCaseView.choices,
        widget=forms.HiddenInput,
    )

    def __init__(
        self,
        project_choices: Sequence[tuple[int, str]],
        run_choices: Sequence[tuple[int, str]],
        data: QueryDict | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        # django-stubs types `self.fields[...]` as the base `Field`; these are
        # the TypedChoiceFields declared above.
        project_field = cast(forms.TypedChoiceField, self.fields["project"])
        run_field = cast(forms.TypedChoiceField, self.fields["run"])
        project_field.choices = [("", "All projects"), *project_choices]
        run_field.choices = [("", "All runs"), *run_choices]
        if not run_choices:
            run_field.disabled = True
            run_field.help_text = "Choose a project to filter by run."

    @staticmethod
    def selected_project_id(
        data: QueryDict | Mapping[str, Any],
        project_choices: Sequence[tuple[int, str]],
    ) -> int | None:
        """The raw ``project`` value when it is one of ``project_choices``
        (used to load the run choices before the form is built)."""
        raw = data.get("project", "")
        try:
            project_id = _coerce_optional_int(str(raw).strip())
        except ValueError:
            return None
        allowed = {choice_id for choice_id, _ in project_choices}
        return project_id if project_id in allowed else None

    def clean(self) -> dict[str, Any] | None:
        cleaned = super().clean()
        date_from = self.cleaned_data.get("date_from")
        date_to = self.cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", "The end date is before the start date.")
        return cleaned

    def to_filters(self) -> FailedCaseFilters:
        """The valid fields as filters (call after ``is_valid``)."""
        data = self.cleaned_data
        return FailedCaseFilters(
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
            project_id=data.get("project"),
            run_id=data.get("run"),
            query=(data.get("q") or "").strip(),
            review=data.get("review") or "",
            failed_by=data.get("failed_by") or "",
        )

    def selected_view(self) -> str:
        """The chosen layout; the case list unless "by_case" is valid."""
        view = self.cleaned_data.get("view") or ""
        return str(view) if view else FailedCaseView.CASES.value


class PivotIdsField(forms.Field):
    """A list of distinct positive run case ids (hidden inputs), at most
    ``BULK_MAX_CASES``; membership is checked by the service."""

    widget = forms.MultipleHiddenInput

    default_error_messages = {
        "required": "Select at least one case.",
        "invalid": "Some selected cases are not valid. Reload the page and try again.",
        "too_many": f"Select at most {BULK_MAX_CASES} cases at a time.",
    }

    def to_python(self, value: Any) -> list[int]:
        if not value:
            return []
        if not isinstance(value, (list, tuple)):
            value = [value]
        ids: list[int] = []
        for item in value:
            try:
                pivot_id = int(item)
            except (TypeError, ValueError) as exc:
                raise forms.ValidationError(
                    self.error_messages["invalid"], code="invalid"
                ) from exc
            if pivot_id <= 0:
                raise forms.ValidationError(
                    self.error_messages["invalid"], code="invalid"
                )
            if pivot_id not in ids:
                ids.append(pivot_id)
        return ids

    def validate(self, value: Any) -> None:
        super().validate(value)
        if len(value) > BULK_MAX_CASES:
            raise forms.ValidationError(
                self.error_messages["too_many"], code="too_many"
            )


class FailedCaseBulkForm(forms.Form):
    """POST of the Failures page bulk bar."""

    action = forms.ChoiceField(label="Action", choices=BulkAction.choices)
    pivot_ids = PivotIdsField(label="Cases")
    # The page's filter query string; re-validated and re-encoded by the view
    # before the redirect, so it can never point off the Failures page.
    return_query = forms.CharField(
        required=False, max_length=RETURN_QUERY_MAX_LENGTH, widget=forms.HiddenInput
    )

    def selected_action(self) -> BulkAction:
        return BulkAction(self.cleaned_data["action"])


class FailedCasePresetForm(forms.Form):
    """ "Save these filters": a name and the current filter query string
    (re-validated by the view before it is stored)."""

    name = forms.CharField(
        label="Name",
        max_length=PRESET_NAME_MAX_LENGTH,
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )
    query = forms.CharField(
        required=False, max_length=PRESET_QUERY_MAX_LENGTH, widget=forms.HiddenInput
    )
