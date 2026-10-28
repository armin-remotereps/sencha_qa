from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.forms import BaseForm
from django.http import QueryDict
from django.utils.text import Truncator

from controller_client.protocol import MAX_VERSION_LENGTH, PersonStepActionKind
from projects.case_checkpoint_services import ResumeStepOption
from projects.models import (
    HELP_NOTE_MAX_LENGTH,
    PERSON_STEP_OUTPUT_MAX_LENGTH,
    PROJECT_PROMPT_MAX_LENGTH,
    ApplicationPlatform,
    PersonStepOutcome,
    Project,
    RunHelpMode,
    TestCaseData,
    TestCasePriority,
    TestCaseType,
)

# Bounds of integer ids and counters fed from a JSON message (BigAutoField
# primary keys, PositiveSmallIntegerField rounds).
_MAX_BIGINT = 2**63 - 1
_MAX_SMALLINT = 32767

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


class ProjectMemberInviteForm(forms.Form):
    email = forms.EmailField(
        label="Email",
        max_length=254,
        help_text=(
            "The person needs an account here. They turn on notifications "
            "in their own Notifications settings."
        ),
        widget=forms.EmailInput(
            attrs={
                "class": "input",
                "placeholder": "colleague@example.com",
                "autocomplete": "off",
            }
        ),
    )


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


RUN_CASE_SELECTION_MAX_IDS = 500


_HELP_MODE_INVALID = "Choose Attended, Night or Human in the loop."


class TestRunStartForm(forms.Form):
    """Start a draft run: what happens when one of its cases needs help."""

    help_mode = forms.ChoiceField(
        label="When a case needs help",
        choices=RunHelpMode.choices,
        initial=RunHelpMode.ATTENDED,
        widget=forms.RadioSelect,
        error_messages={
            "required": "Choose what happens when a case needs help.",
            "invalid_choice": _HELP_MODE_INVALID,
        },
    )


class RunHelpModeForm(forms.Form):
    """Optional help mode of a Resume or Re-run; blank keeps the run's mode."""

    help_mode = forms.ChoiceField(
        label="When a case needs help",
        choices=RunHelpMode.choices,
        required=False,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={"invalid_choice": _HELP_MODE_INVALID},
    )

    def clean_help_mode(self) -> str | None:
        value: str = self.cleaned_data.get("help_mode") or ""
        return value or None


class RunHelpModeChangeForm(forms.Form):
    """Switch an active run's help mode; applies from the next Need help."""

    help_mode = forms.ChoiceField(
        label="When a case needs help",
        choices=RunHelpMode.choices,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": "Choose what happens when a case needs help.",
            "invalid_choice": _HELP_MODE_INVALID,
        },
    )


class CaseHelpResolutionForm(forms.Form):
    """Continue / Mark failed / Mark blocked on a Need help case, with an optional note."""

    note = forms.CharField(
        label="Note for the agent",
        required=False,
        max_length=HELP_NOTE_MAX_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": "Optional: what you fixed, or what the agent should know",
            }
        ),
    )
    # The help-chat redesign shown with the form; None when none was shown.
    plan_revision_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )
    use_plan_revision = forms.BooleanField(
        label="Use the redesigned plan",
        required=False,
        initial=True,
    )
    # The help-chat thread to return to; checked by the service.
    return_thread_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class PersonStepReportForm(forms.Form):
    """A person's report of a step they performed (Human in the loop)."""

    # The open step the card was rendered for; the service refuses a stale id.
    person_step_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    outcome = forms.ChoiceField(
        label="How did it go?",
        choices=PersonStepOutcome.choices,
        initial=PersonStepOutcome.AS_EXPECTED,
        widget=forms.RadioSelect,
        error_messages={
            "required": "Choose how the step went.",
            "invalid_choice": "Choose how the step went.",
        },
    )
    note = forms.CharField(
        label="Note for the agent",
        required=False,
        max_length=HELP_NOTE_MAX_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": "What you did and what you saw",
            }
        ),
    )
    # Command steps only; pasted from the test machine's shell.
    output = forms.CharField(
        label="Command output",
        required=False,
        max_length=PERSON_STEP_OUTPUT_MAX_LENGTH,
        strip=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 6,
                "placeholder": "Paste the command's output",
            }
        ),
    )

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        if cleaned_data.get(
            "outcome"
        ) == PersonStepOutcome.DIFFERENT and not cleaned_data.get("note"):
            self.add_error("note", "Describe what was different.")
        return cleaned_data


class PersonStepDecisionForm(forms.Form):
    """Overrule / Mark failed / Mark blocked on a person step, with an optional note."""

    person_step_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    note = forms.CharField(
        label="Note",
        required=False,
        max_length=HELP_NOTE_MAX_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": "Optional: why",
            }
        ),
    )


class _WireTextField(forms.CharField):
    """A text field fed from a JSON message: only a string (or nothing) is valid.

    ``CharField`` would turn a list or a number into its ``str()``.
    """

    def to_python(self, value: Any) -> str:
        if value is not None and not isinstance(value, str):
            raise forms.ValidationError("Expected text.", code="invalid")
        return cast(str, super().to_python(value))


class RunnerPersonStepActionForm(forms.Form):
    """A person's answer to a person step sent from the Test Runner window.

    Fed from the ``person_step_action`` message (not a rendered form). The same
    rules as ``PersonStepReportForm`` / ``PersonStepDecisionForm``; over-long
    input is refused, not cut.
    """

    action = forms.ChoiceField(
        choices=[(kind.value, kind.value) for kind in PersonStepActionKind]
    )
    person_step_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    # The round the window showed; ``CasePersonStep.round`` is a small int.
    round = forms.IntegerField(min_value=1, max_value=_MAX_SMALLINT)
    # Report only; the runner sends "" for the other actions.
    outcome = forms.ChoiceField(
        required=False,
        choices=PersonStepOutcome.choices,
        error_messages={"invalid_choice": "Choose how the step went."},
    )
    note = _WireTextField(required=False, max_length=HELP_NOTE_MAX_LENGTH, strip=True)
    output = _WireTextField(
        required=False, max_length=PERSON_STEP_OUTPUT_MAX_LENGTH, strip=False
    )

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        if cleaned_data.get("action") != PersonStepActionKind.REPORT:
            cleaned_data["outcome"] = ""
            cleaned_data["output"] = ""
            return cleaned_data
        outcome = cleaned_data.get("outcome")
        if "outcome" not in self.errors and not outcome:
            self.add_error("outcome", "Choose how the step went.")
        if outcome == PersonStepOutcome.DIFFERENT and not cleaned_data.get("note"):
            self.add_error("note", "Describe what was different.")
        return cleaned_data


class HelpReturnForm(forms.Form):
    """Discard a help-chat redesign; optionally return to the help-chat thread."""

    return_thread_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class RunCaseSelectionForm(forms.Form):
    """Selected run cases (pivot ids) for Check / Uncheck / Re-run on a test run.

    The caller supplies the allowed ids (the run's checkable, uncheckable or
    re-runnable pivots), so the choices double as membership validation.
    """

    pivot_ids = forms.TypedMultipleChoiceField(
        label="Cases",
        coerce=int,
        choices=(),
        required=True,
        widget=forms.MultipleHiddenInput,
        error_messages={
            "required": "Select at least one case.",
            "invalid_choice": "Some selected cases can no longer be changed. "
            "Reload the page and try again.",
        },
    )

    def __init__(
        self,
        allowed_ids: Sequence[int],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        # django-stubs types `self.fields[...]` as the base `Field`; this is
        # the TypedMultipleChoiceField declared above.
        pivot_ids_field = cast(forms.TypedMultipleChoiceField, self.fields["pivot_ids"])
        pivot_ids_field.choices = [(pid, str(pid)) for pid in allowed_ids]

    def clean_pivot_ids(self) -> list[int]:
        pivot_ids: list[int] = self.cleaned_data["pivot_ids"]
        if len(pivot_ids) > RUN_CASE_SELECTION_MAX_IDS:
            raise forms.ValidationError(
                f"Select at most {RUN_CASE_SELECTION_MAX_IDS} cases at a time."
            )
        return pivot_ids


RESUME_STEP_LABEL_MAX_CHARS = 80


class RunCasesRerunForm(RunCaseSelectionForm, RunHelpModeForm):
    """Re-run selected cases in place, optionally changing the run's help mode."""


def _resume_step_label(option: ResumeStepOption, *, is_last: bool) -> str:
    if option.verdict_only:
        return "After the last step: only write the verdict"
    description = Truncator(option.description).chars(RESUME_STEP_LABEL_MAX_CHARS)
    label = f"Step {option.step_num}: {description}"
    if option.passed:
        return f"{label} (passed)"
    if is_last:
        return f"{label} (stopped here)"
    return label


class CaseResumeForm(RunHelpModeForm):
    """Step a resumed case continues from (Resume case dialog on the case page).

    The caller supplies the case's checkpoint steps (``list_resume_steps``),
    so the choices double as validation; the last option, the step that was
    interrupted, is the default. Without a checkpoint the view validates only
    ``RunHelpModeForm``. The optional ``help_mode`` is inherited.
    """

    from_step = forms.TypedChoiceField(
        label="Continue from",
        coerce=int,
        choices=(),
        required=True,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": "Choose the step to continue from.",
            "invalid_choice": "That step can no longer be resumed. "
            "Reload the page and try again.",
        },
    )

    def __init__(
        self,
        step_options: Sequence[ResumeStepOption],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        # django-stubs types `self.fields[...]` as the base `Field`; this is
        # the TypedChoiceField declared above.
        from_step_field = cast(forms.TypedChoiceField, self.fields["from_step"])
        last_index = len(step_options) - 1
        from_step_field.choices = [
            (option.step_num, _resume_step_label(option, is_last=i == last_index))
            for i, option in enumerate(step_options)
        ]
        if step_options:
            from_step_field.initial = step_options[-1].step_num


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


class TestRailRunImportForm(forms.Form):
    """Import the cases of one TestRail run, pasted as a run URL or ID."""

    run_reference = forms.CharField(max_length=2000, strip=True)
    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)


class ContextRestoreForm(forms.Form):
    """Restore one application context version; ``next`` is checked same-host."""

    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)


class TestRailPushStartForm(forms.Form):
    """Step 1 of pushing a run's results to TestRail.

    A blank `testrail_project_id` means "push using the run's existing
    TestRail target"; a value means "the user just picked a project".
    """

    testrail_project_id = forms.IntegerField(required=False, min_value=1)


class RunnerUpdateForm(forms.Form):
    """Environment page: update the connected Test Runner.

    ``expected_version`` is the server version the page showed; the service
    refuses it if the server has since been redeployed with another version.
    """

    expected_version = forms.CharField(
        max_length=MAX_VERSION_LENGTH, strip=True, widget=forms.HiddenInput
    )
