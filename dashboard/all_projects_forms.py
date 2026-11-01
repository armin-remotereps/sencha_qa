from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

from django import forms

from dashboard.all_projects_types import (
    ALL_PROJECTS_QUERY_MAX_LENGTH,
    AllProjectsFilters,
    AllProjectsSort,
    MembershipFilter,
    ProjectStatusFilter,
    RunActivityFilter,
    RunnerFilter,
)

_DEFAULTS = AllProjectsFilters()


class AllProjectsFilterForm(forms.Form):
    """GET filters of the superuser "All projects" page; every field optional.

    An invalid field is reported and falls back to its default (the rest
    still apply).
    """

    q = forms.CharField(
        label="Search",
        required=False,
        # No max_length: longer text is cut by the service, not refused.
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "maxlength": ALL_PROJECTS_QUERY_MAX_LENGTH,
                "type": "search",
                "placeholder": "Project name, member email or tag",
            }
        ),
    )
    status = forms.ChoiceField(
        label="Status",
        required=False,
        choices=[(item.value, item.label) for item in ProjectStatusFilter],
        widget=forms.Select(attrs={"class": "select"}),
    )
    runner = forms.ChoiceField(
        label="Test Runner",
        required=False,
        choices=[(item.value, item.label) for item in RunnerFilter],
        widget=forms.Select(attrs={"class": "select"}),
    )
    run = forms.ChoiceField(
        label="Runs",
        required=False,
        choices=[(item.value, item.label) for item in RunActivityFilter],
        widget=forms.Select(attrs={"class": "select"}),
    )
    membership = forms.ChoiceField(
        label="My membership",
        required=False,
        choices=[(item.value, item.label) for item in MembershipFilter],
        widget=forms.Select(attrs={"class": "select"}),
    )
    sort = forms.ChoiceField(
        label="Sort by",
        required=False,
        choices=[(item.value, item.label) for item in AllProjectsSort],
        widget=forms.Select(attrs={"class": "select"}),
    )

    def filters(self) -> AllProjectsFilters:
        """The valid values, defaults for invalid or missing ones."""
        # An invalid bound form keeps only its valid fields in ``cleaned_data``;
        # an unbound form has none.
        self.is_valid()
        # Every field is a CharField or ChoiceField, so cleaned values are str.
        cleaned: Mapping[str, str] = getattr(self, "cleaned_data", {})
        query = cleaned.get("q")
        status = cleaned.get("status")
        runner = cleaned.get("runner")
        run = cleaned.get("run")
        membership = cleaned.get("membership")
        sort = cleaned.get("sort")
        return AllProjectsFilters(
            query=(query or "").strip(),
            status=ProjectStatusFilter(status) if status else _DEFAULTS.status,
            runner=RunnerFilter(runner) if runner else _DEFAULTS.runner,
            run=RunActivityFilter(run) if run else _DEFAULTS.run,
            membership=(
                MembershipFilter(membership) if membership else _DEFAULTS.membership
            ),
            sort=AllProjectsSort(sort) if sort else _DEFAULTS.sort,
        )


def encode_all_projects_filters(filters: AllProjectsFilters) -> str:
    """The query string of the non-default filters (no leading ``?``; never
    page or per_page)."""
    params: list[tuple[str, str]] = []
    if filters.query:
        params.append(("q", filters.query))
    for key, value, default in (
        ("status", filters.status, _DEFAULTS.status),
        ("runner", filters.runner, _DEFAULTS.runner),
        ("run", filters.run, _DEFAULTS.run),
        ("membership", filters.membership, _DEFAULTS.membership),
        ("sort", filters.sort, _DEFAULTS.sort),
    ):
        if value != default:
            params.append((key, value.value))
    return urlencode(params)
