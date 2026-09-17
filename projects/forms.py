from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.forms import BaseForm
from django.http import QueryDict

from projects.models import (
    ApplicationPlatform,
    Project,
    TestCaseData,
    TestCasePriority,
    TestCaseType,
)

PROJECT_PROMPT_MAX_LENGTH = 4000

_APPLICATION_PLATFORM_CHOICES: list[tuple[str, str]] = [("", "Not specified")] + list(
    ApplicationPlatform.choices
)


class ProjectForm(forms.Form):
    name = forms.CharField(
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "Project name",
            }
        ),
    )
    tags = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input",
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
                "class": "input",
                "placeholder": "Test case title",
            }
        ),
    )
    testrail_id = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input",
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
                "class": "input",
                "placeholder": "Template",
            }
        ),
    )
    type = forms.ChoiceField(
        choices=TestCaseType.choices,
        initial=TestCaseType.FUNCTIONAL,
        widget=forms.Select(attrs={"class": "select"}),
    )
    priority = forms.ChoiceField(
        choices=TestCasePriority.choices,
        initial=TestCasePriority.MUST_TEST_HIGH,
        widget=forms.Select(attrs={"class": "select"}),
    )
    estimate = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "e.g. 30m, 1h",
            }
        ),
    )
    references = forms.CharField(
        max_length=500,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "e.g. JIRA-123",
            }
        ),
    )
    preconditions = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": "Preconditions...",
            }
        ),
    )
    steps = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": "Steps...",
            }
        ),
    )
    expected = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
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


class ApplicationContextForm(forms.Form):
    """Edits a project's application context — the standalone edit page and
    the wizard's "Application context" step share this same form."""

    application_url = forms.URLField(
        label="Application URL",
        required=False,
        max_length=500,
        widget=forms.URLInput(
            attrs={
                "class": "input",
                "placeholder": "https://app.example.com",
            }
        ),
    )
    application_platform = forms.ChoiceField(
        label="Platform / application type",
        required=False,
        choices=_APPLICATION_PLATFORM_CHOICES,
        widget=forms.Select(attrs={"class": "select"}),
    )
    project_prompt = forms.CharField(
        label="Notes",
        required=False,
        max_length=PROJECT_PROMPT_MAX_LENGTH,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 8,
                "placeholder": (
                    "Environment, navigation, authentication notes, test "
                    "accounts — anything the AI agent should know about "
                    "the application under test..."
                ),
            }
        ),
    )


class TestRunCreateForm(forms.Form):
    """Selects test cases and (optionally) names a new run.

    Used both by the Test Cases page's "create run" dialog and the
    onboarding wizard's final step. The `test_case_ids` choices double as
    membership validation: an id outside the project is rejected.
    """

    name = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "placeholder": "Run name",
            }
        ),
    )
    test_case_ids = forms.TypedMultipleChoiceField(
        coerce=int,
        choices=(),
        required=True,
        widget=forms.MultipleHiddenInput,
        error_messages={"required": "Select at least one test case."},
    )

    def __init__(
        self,
        project: Project,
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        # django-stubs types `self.fields[...]` as the base `Field`, which has
        # no `choices` attribute; this field is always the
        # TypedMultipleChoiceField declared above.
        test_case_ids_field = cast(
            forms.TypedMultipleChoiceField, self.fields["test_case_ids"]
        )
        test_case_ids_field.choices = [
            (case_id, str(case_id))
            for case_id in project.test_cases.values_list("id", flat=True)
        ]


def _field_label(form: BaseForm, field_name: str) -> str:
    if field_name == NON_FIELD_ERRORS:
        return ""
    field = form.fields.get(field_name)
    if field is not None and field.label:
        return str(field.label)
    return field_name.replace("_", " ").title()


def form_error_text(form: BaseForm) -> str:
    """Join a form's field errors into one human-readable string for `messages`."""
    parts: list[str] = []
    for field_name, errors in form.errors.items():
        label = _field_label(form, field_name)
        for error in errors:
            message = str(error)
            parts.append(f"{label}: {message}" if label else message)
    return " ".join(parts)


class TestRailSettingsForm(forms.Form):
    """Settings tab: TestRail URL, account email, and API key.

    The API key is never pre-filled (see `has_stored_key` below) — once a
    key is stored, leaving the field blank on a later save keeps it.
    """

    testrail_url = forms.URLField(
        label="TestRail URL",
        max_length=500,
        assume_scheme="https",
        widget=forms.URLInput(
            attrs={"class": "input", "placeholder": "https://yourteam.testrail.com"}
        ),
    )
    testrail_email = forms.EmailField(
        label="Account email",
        widget=forms.EmailInput(
            attrs={"class": "input", "placeholder": "you@company.com"}
        ),
    )
    testrail_api_key = forms.CharField(
        label="API key",
        required=False,
        widget=forms.PasswordInput(
            attrs={"class": "input", "autocomplete": "off"}, render_value=False
        ),
    )

    def __init__(self, *args: Any, has_stored_key: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.has_stored_key = has_stored_key

    def clean_testrail_url(self) -> str:
        url: str = self.cleaned_data["testrail_url"].strip()
        if not url.lower().startswith(("https://", "http://")):
            raise forms.ValidationError(
                "Enter a valid URL starting with https:// or http://."
            )
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise forms.ValidationError(
                "Do not put credentials in the URL; use the email and API key fields."
            )
        # The client appends "/index.php?/api/v2/..." to this base. A query or
        # fragment here would turn that fixed API path into query noise and let
        # the stored URL point requests at an arbitrary path on the host.
        if parsed.query or parsed.fragment:
            raise forms.ValidationError(
                "Enter the TestRail base URL only, without a query string or fragment."
            )
        return url.rstrip("/")

    def clean_testrail_api_key(self) -> str | None:
        """Empty means "keep the stored key" once one exists; required otherwise."""
        raw: str = self.cleaned_data.get("testrail_api_key", "").strip()
        if raw:
            return raw
        if self.has_stored_key:
            return None
        raise forms.ValidationError("API key is required.")


class TestRailImportStartForm(forms.Form):
    testrail_project_id = forms.IntegerField(min_value=1)


class TestRailSuiteForm(forms.Form):
    testrail_suite_id = forms.IntegerField(min_value=1)


class TestRailPushStartForm(forms.Form):
    """Step 1 of pushing a run's results to TestRail.

    A blank `testrail_project_id` means "push using the run's existing
    TestRail target"; a value means "the user just picked a project".
    """

    testrail_project_id = forms.IntegerField(required=False, min_value=1)
