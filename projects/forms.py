from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import NON_FIELD_ERRORS
from django.core.validators import RegexValidator
from django.forms import BaseForm
from django.http import QueryDict
from django.utils.text import Truncator

from controller_client.protocol import (
    CAPTURE_ALLOWED_PROCESSES_MAX,
    CAPTURE_PROCESS_NAME_MAX_CHARS,
    MANUAL_CLAIM_TOKEN_MAX_CHARS,
    PERSON_CHAT_TEXT_MAX_CHARS,
    ManualCaseActionKind,
    PersonHelpActionKind,
    PersonStepActionKind,
    normalize_process_name,
)
from projects.case_checkpoint_services import ResumeStepOption
from projects.models import (
    HELP_NOTE_MAX_LENGTH,
    PAUSE_MESSAGE_MAX_LENGTH,
    PERSON_STEP_OUTPUT_MAX_LENGTH,
    PROJECT_PROMPT_MAX_LENGTH,
    RETEST_NOTE_MAX_LENGTH,
    ApplicationPlatform,
    HelpContinueMode,
    PersonStepOutcome,
    Project,
    ProjectRole,
    ProjectRunModeScope,
    RunHelpMode,
    TestCaseData,
    TestCasePriority,
    TestCaseType,
)
from runners.types import RunnerId

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
    read_only = forms.BooleanField(
        label="Read-only",
        required=False,
        help_text="They can view the project but not change anything.",
    )


class ProjectMemberRoleForm(forms.Form):
    """Change one member's role on the Settings Members card."""

    role = forms.ChoiceField(
        label="Role",
        choices=ProjectRole.choices,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": "Choose a role.",
            "invalid_choice": "Choose a valid role.",
        },
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
_RUNNER_REQUIRED = "Choose a Test Runner."
_RUNNER_INVALID = "Choose one of your Test Runners. Reload the page and try again."


class _RunnerOptionForm(forms.Form):
    """Base: an optional ``runner``, one of the viewer's own Test Runners.

    The caller passes ``runner_choices`` (``projects.runner_choices``), which
    double as validation: any other runner id is refused. Blank keeps the
    run's runner (``None``); ``runner_required`` (the run has none) makes it
    required. Without ``runner_choices`` (an action that takes no runner, such
    as Mark failed sharing the Continue form) the field is dropped and a
    posted runner is ignored. Whether the runner may serve the run now is the
    service's.
    """

    runner = forms.TypedChoiceField(
        label="Test Runner",
        coerce=int,
        empty_value=None,
        required=False,
        choices=(),
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": _RUNNER_REQUIRED,
            "invalid_choice": _RUNNER_INVALID,
        },
    )

    def __init__(
        self,
        *args: Any,
        runner_choices: Sequence[tuple[int, str]] | None = None,
        runner_required: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if runner_choices is None:
            del self.fields["runner"]
            return
        # django-stubs types `self.fields[...]` as the base `Field`; this is
        # the TypedChoiceField declared above.
        runner_field = cast(forms.TypedChoiceField, self.fields["runner"])
        runner_field.required = runner_required
        runner_field.choices = [("", ""), *runner_choices]

    def clean_runner(self) -> RunnerId | None:
        value = self.cleaned_data.get("runner")
        return RunnerId(value) if isinstance(value, int) else None


class TestRunStartForm(_RunnerOptionForm):
    """Start a draft run: its Test Runner and what happens when a case needs help.

    ``help_mode`` is optional: blank keeps the run's stored mode
    (``stored_help_mode``). A runner is required unless the run is Manual
    (Q12: optional there).
    """

    help_mode = forms.ChoiceField(
        label="When a case needs help",
        choices=RunHelpMode.choices,
        initial=RunHelpMode.HUMAN_IN_THE_LOOP,
        required=False,
        widget=forms.RadioSelect,
        error_messages={"invalid_choice": _HELP_MODE_INVALID},
    )

    def __init__(
        self,
        *args: Any,
        stored_help_mode: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._stored_help_mode = stored_help_mode

    def clean_help_mode(self) -> str | None:
        # Blank keeps the run's stored mode (only superusers are offered a choice).
        value: str = self.cleaned_data.get("help_mode") or ""
        return value or None

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        help_mode = cleaned_data.get("help_mode") or self._stored_help_mode
        if (
            "runner" in self.fields
            and "runner" not in self.errors
            and cleaned_data.get("runner") is None
            and help_mode != RunHelpMode.MANUAL
        ):
            self.add_error("runner", _RUNNER_REQUIRED)
        return cleaned_data


class RunRunnerForm(forms.Form):
    """Run page "Test Runner" card: assign or change the run's runner (Q6)."""

    runner = forms.TypedChoiceField(
        label="Test Runner",
        coerce=int,
        choices=(),
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": _RUNNER_REQUIRED,
            "invalid_choice": _RUNNER_INVALID,
        },
    )

    def __init__(
        self,
        runner_choices: Sequence[tuple[int, str]],
        data: QueryDict | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(data, **kwargs)
        runner_field = cast(forms.TypedChoiceField, self.fields["runner"])
        runner_field.choices = list(runner_choices)

    def clean_runner(self) -> RunnerId:
        return RunnerId(self.cleaned_data["runner"])


class RunHelpModeForm(_RunnerOptionForm):
    """Optional help mode and Test Runner of a Resume or Re-run.

    Blank keeps the run's mode / runner.
    """

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


class ProjectRunModeForm(forms.Form):
    """A superuser sets the project's run mode and which existing runs follow it."""

    help_mode = forms.ChoiceField(
        label="Run mode",
        choices=RunHelpMode.choices,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={
            "required": "Choose a run mode.",
            "invalid_choice": _HELP_MODE_INVALID,
        },
    )
    apply_to = forms.ChoiceField(
        label="Apply to",
        choices=[
            (ProjectRunModeScope.NEW_ONLY.value, "New runs only"),
            (ProjectRunModeScope.DRAFTS.value, "New runs and drafts"),
            (ProjectRunModeScope.OPEN.value, "Also running and paused runs"),
            (ProjectRunModeScope.ALL.value, "All runs, including finished ones"),
        ],
        initial=ProjectRunModeScope.DRAFTS.value,
        widget=forms.RadioSelect,
        error_messages={
            "required": "Choose which runs to apply the mode to.",
            "invalid_choice": "Choose which runs to apply the mode to.",
        },
    )

    def clean_apply_to(self) -> ProjectRunModeScope:
        return ProjectRunModeScope(self.cleaned_data["apply_to"])


# Longest textarea accepted: the cap times a generous line length.
_CAPTURE_APPS_MAX_CHARS = CAPTURE_ALLOWED_PROCESSES_MAX * 300


class ProjectCaptureAppsForm(forms.Form):
    """Settings "Screenshot capture": the applications that may be captured.

    ``cleaned_data["apps"]`` is the list of non-blank lines, stripped and in
    order; ``project_capture_apps_services`` normalizes and de-duplicates them.
    """

    apps = forms.CharField(
        label="Allowed applications",
        required=False,
        strip=True,
        max_length=_CAPTURE_APPS_MAX_CHARS,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 6,
                "spellcheck": "false",
                "autocapitalize": "off",
                "autocomplete": "off",
            }
        ),
        error_messages={
            "max_length": "The list is too long.",
        },
    )

    def clean_apps(self) -> list[str]:
        text: str = self.cleaned_data["apps"]
        lines: list[str] = []
        errors: list[forms.ValidationError] = []
        names: set[str] = set()
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            lines.append(line)
            name = normalize_process_name(line)
            if name is None:
                errors.append(
                    forms.ValidationError(
                        "Line %(number)s: %(line)s is not a valid application "
                        "name. Use letters, digits, spaces and . _ + - only.",
                        params={
                            "number": number,
                            "line": Truncator(line).chars(60),
                        },
                    )
                )
            else:
                names.add(name)
        if len(names) > CAPTURE_ALLOWED_PROCESSES_MAX:
            errors.append(
                forms.ValidationError(
                    f"List at most {CAPTURE_ALLOWED_PROCESSES_MAX} applications "
                    f"(you listed {len(names)})."
                )
            )
        if errors:
            raise forms.ValidationError(errors)
        return lines


class ProjectCaptureAppsAddForm(forms.Form):
    """Settings "Screenshot capture": the phone apps ticked in the picker.

    ``apps`` is every submitted ``apps`` value (``getlist``); the template
    renders the checkboxes itself. ``cleaned_data["apps"]`` holds the ids in
    order without duplicates; each must already be a normalized app id
    (``normalize_process_name`` returns it unchanged), as the picker lists
    them. ``project_capture_apps_services.add_capture_allowed_processes``
    checks the cap against the stored list.
    """

    apps = forms.Field(
        required=True,
        widget=forms.MultipleHiddenInput,
        error_messages={"required": "Tick at least one app."},
    )

    def clean_apps(self) -> list[str]:
        raw: object = self.cleaned_data["apps"]
        values = raw if isinstance(raw, list) else [raw]
        app_ids: list[str] = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or len(value) > CAPTURE_PROCESS_NAME_MAX_CHARS
                or normalize_process_name(value) != value
            ):
                raise forms.ValidationError(
                    "The selection contains an invalid app id. Load the list again."
                )
            if value not in app_ids:
                app_ids.append(value)
        if len(app_ids) > CAPTURE_ALLOWED_PROCESSES_MAX:
            raise forms.ValidationError(
                f"Tick at most {CAPTURE_ALLOWED_PROCESSES_MAX} apps "
                f"(you ticked {len(app_ids)})."
            )
        return app_ids


class CaseHelpResolutionForm(_RunnerOptionForm):
    """Continue / Mark failed / Mark blocked on a Need help case, with an optional note.

    ``runner`` (inherited, optional) is read by Continue only: blank keeps
    the run's Test Runner.
    """

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
    # How Continue goes on; blank keeps the request's stored mode. The
    # service checks it against the modes the request allows.
    continue_mode = forms.ChoiceField(
        label="How to continue",
        required=False,
        choices=HelpContinueMode.choices,
        widget=forms.RadioSelect,
    )

    def clean_continue_mode(self) -> HelpContinueMode | None:
        value = self.cleaned_data.get("continue_mode")
        if not value:
            return None
        return HelpContinueMode(value)


class CasePauseForm(forms.Form):
    """Pause a running case (or a waiting person step), with a first message."""

    message = forms.CharField(
        label="Message for the agent",
        required=False,
        max_length=PAUSE_MESSAGE_MAX_LENGTH,
        strip=True,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 4,
                "placeholder": "Optional: what you want to discuss once it stops",
            }
        ),
    )
    # Where to go back to; the view keeps it only when it stays on this host.
    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)


class PersonStepReportForm(forms.Form):
    """A person's report of a step they performed (Human in the loop).

    No runner choice: the agent confirms on the run's own Test Runner, where
    the step was performed (Change runner on the run card hands it back).
    """

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
    # Mark failed / Mark blocked only: the pasted command output, kept as the
    # evidence the agent records (the card's shared form posts it).
    output = forms.CharField(
        required=False, max_length=PERSON_STEP_OUTPUT_MAX_LENGTH, strip=False
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
        action = cleaned_data.get("action")
        if action != PersonStepActionKind.REPORT:
            cleaned_data["outcome"] = ""
            # Mark failed / blocked keep the output as evidence.
            if action not in (PersonStepActionKind.FAIL, PersonStepActionKind.BLOCK):
                cleaned_data["output"] = ""
            return cleaned_data
        outcome = cleaned_data.get("outcome")
        if "outcome" not in self.errors and not outcome:
            self.add_error("outcome", "Choose how the step went.")
        if outcome == PersonStepOutcome.DIFFERENT and not cleaned_data.get("note"):
            self.add_error("note", "Describe what was different.")
        return cleaned_data


# A server-minted claim token as the protocol allows it.
_claim_token_validator = RegexValidator(
    r"\A[A-Za-z0-9_-]+\Z", message="Invalid claim.", code="invalid"
)


class RunnerManualCaseActionForm(forms.Form):
    """A claim, release, pause or continue of a Manual run case sent from the
    Test Runner window.

    Fed from the ``manual_case_action`` message (not a rendered form).
    ``claim_token`` is required for every action but a claim, and ignored for
    a claim.
    """

    action = forms.ChoiceField(
        choices=[(kind.value, kind.value) for kind in ManualCaseActionKind]
    )
    pivot_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    claim_token = _WireTextField(
        required=False,
        max_length=MANUAL_CLAIM_TOKEN_MAX_CHARS,
        strip=False,
        validators=[_claim_token_validator],
    )

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        if cleaned_data.get("action") == ManualCaseActionKind.CLAIM:
            cleaned_data["claim_token"] = ""
        elif "claim_token" not in self.errors and not cleaned_data.get("claim_token"):
            self.add_error("claim_token", "Missing claim.")
        return cleaned_data


class RunnerManualChatSendForm(forms.Form):
    """A discussion turn about the claimed Manual run case (Test Runner window).

    Fed from the ``manual_chat_send`` message (not a rendered form); the same
    limit as a web setup message (restated by the protocol).
    """

    pivot_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    claim_token = _WireTextField(
        max_length=MANUAL_CLAIM_TOKEN_MAX_CHARS,
        strip=False,
        validators=[_claim_token_validator],
    )
    text = _WireTextField(max_length=PERSON_CHAT_TEXT_MAX_CHARS, strip=True)


class RunnerCasePauseForm(forms.Form):
    """A person's pause of a case sent from the Test Runner window.

    Fed from the ``case_pause_action`` message (not a rendered form). The same
    message rule as the web pause dialog; an over-long message is refused.
    """

    pivot_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    message = _WireTextField(
        required=False, max_length=PAUSE_MESSAGE_MAX_LENGTH, strip=True
    )


class RunnerChatSendForm(forms.Form):
    """A person's discussion turn sent from the Test Runner window.

    Fed from the ``person_chat_send`` message (not a rendered form); the same
    limit as a web help-chat message (``SETUP_MESSAGE_MAX_LENGTH``, restated
    by the protocol).
    """

    pivot_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    # The open request the window showed; a stale card is refused.
    help_request_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    text = _WireTextField(max_length=PERSON_CHAT_TEXT_MAX_CHARS, strip=True)


class RunnerHelpActionForm(forms.Form):
    """A person's help resolution sent from the Test Runner window.

    Fed from the ``person_help_action`` message (not a rendered form). The
    same fields as ``CaseHelpResolutionForm``; ``continue_mode``,
    ``plan_revision_id`` and ``use_plan_revision`` apply to Continue only.
    """

    pivot_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    help_request_id = forms.IntegerField(min_value=1, max_value=_MAX_BIGINT)
    action = forms.ChoiceField(
        choices=[(kind.value, kind.value) for kind in PersonHelpActionKind]
    )
    note = _WireTextField(required=False, max_length=HELP_NOTE_MAX_LENGTH, strip=True)
    continue_mode = forms.ChoiceField(
        required=False,
        choices=HelpContinueMode.choices,
        error_messages={"invalid_choice": "Choose how the case continues."},
    )
    # 0 (or absent): no redesign was shown.
    plan_revision_id = forms.IntegerField(
        required=False, min_value=0, max_value=_MAX_BIGINT
    )
    use_plan_revision = forms.BooleanField(required=False)

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        if cleaned_data.get("action") != PersonHelpActionKind.CONTINUE:
            cleaned_data["continue_mode"] = ""
            cleaned_data["plan_revision_id"] = None
            cleaned_data["use_plan_revision"] = False
        elif not cleaned_data.get("plan_revision_id"):
            cleaned_data["plan_revision_id"] = None
        return cleaned_data


class ManualReleaseForm(forms.Form):
    """Release a stuck Manual run claim from the web; ``next`` is where to go
    back to (validated same-host by the view)."""

    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)


class ManualPauseForm(forms.Form):
    """Pause or continue the claimed Manual run case from the web; ``next`` is
    where to go back to (validated same-host by the view)."""

    next = forms.CharField(required=False, max_length=2000, widget=forms.HiddenInput)


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


class TestCaseRefreshForm(forms.Form):
    """Refresh one test case from TestRail; ``next`` is checked same-host."""

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


class CapturePhoneAppsLoadForm(forms.Form):
    """Settings "Screenshot capture": the phone runner to list the apps of.

    Only an id: ``capture_phone_apps_services.load_phone_apps`` refuses a
    runner that is not the viewer's own connected phone runner.
    """

    runner = forms.IntegerField(
        min_value=1,
        max_value=_MAX_BIGINT,
        error_messages={
            "required": "Pick one of your Test Runners.",
            "invalid": "Pick one of your Test Runners.",
        },
    )

    def clean_runner(self) -> RunnerId:
        return RunnerId(self.cleaned_data["runner"])


class RunCasesRetestForm(RunCaseSelectionForm):
    """Selected run cases to mark Needs re-test, with an optional note."""

    note = forms.CharField(
        label="Note",
        required=False,
        strip=True,
        max_length=RETEST_NOTE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
