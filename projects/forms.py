from __future__ import annotations

from django import forms

from accounts.forms import FIELD_CSS
from projects.models import TestCaseData, TestCasePriority, TestCaseType

TEXTAREA_CSS = (
    "w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 "
    "text-sm text-zinc-100 placeholder:text-zinc-400 "
    "focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2 "
    "focus:ring-offset-zinc-950"
)

SELECT_CSS = (
    "flex h-10 w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 "
    "text-sm text-zinc-100 "
    "focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:ring-offset-2 "
    "focus:ring-offset-zinc-950"
)


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


class TestCaseForm(forms.Form):
    title = forms.CharField(
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "Test case title",
            }
        ),
    )
    testrail_id = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "e.g. C12345",
            }
        ),
    )
    template = forms.CharField(
        max_length=100,
        required=False,
        initial="Test Case",
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "Template",
            }
        ),
    )
    type = forms.ChoiceField(
        choices=TestCaseType.choices,
        initial=TestCaseType.FUNCTIONAL,
        widget=forms.Select(attrs={"class": SELECT_CSS}),
    )
    priority = forms.ChoiceField(
        choices=TestCasePriority.choices,
        initial=TestCasePriority.MUST_TEST_HIGH,
        widget=forms.Select(attrs={"class": SELECT_CSS}),
    )
    estimate = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "e.g. 30m, 1h",
            }
        ),
    )
    references = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": FIELD_CSS,
                "placeholder": "e.g. JIRA-123",
            }
        ),
    )
    preconditions = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": TEXTAREA_CSS,
                "rows": 3,
                "placeholder": "Preconditions...",
            }
        ),
    )
    steps = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": TEXTAREA_CSS,
                "rows": 3,
                "placeholder": "Steps...",
            }
        ),
    )
    expected = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": TEXTAREA_CSS,
                "rows": 3,
                "placeholder": "Expected result...",
            }
        ),
    )

    def to_data(self) -> TestCaseData:
        cd = self.cleaned_data
        return TestCaseData(
            title=cd["title"],
            testrail_id=cd.get("testrail_id", ""),
            template=cd.get("template", "") or "Test Case",
            type=cd["type"],
            priority=cd["priority"],
            estimate=cd.get("estimate", ""),
            references=cd.get("references", ""),
            preconditions=cd.get("preconditions", ""),
            steps=cd.get("steps", ""),
            expected=cd.get("expected", ""),
        )
