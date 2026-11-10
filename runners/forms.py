"""Forms of the ``/runners/`` pages. Name rules live in ``runners.names``
(the services validate again); these forms only bound the input."""

from __future__ import annotations

from django import forms

from controller_client.protocol import MAX_VERSION_LENGTH
from runners.models import RUNNER_NAME_MAX_LENGTH


class RunnerCreateForm(forms.Form):
    """Add a Test Runner: its name (unique per owner, case-insensitive)."""

    name = forms.CharField(
        max_length=RUNNER_NAME_MAX_LENGTH,
        strip=True,
        label="Name",
        help_text="For example the machine it runs on: “Office laptop”, “Pixel 8 host”.",
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )


class RunnerRenameForm(forms.Form):
    name = forms.CharField(
        max_length=RUNNER_NAME_MAX_LENGTH,
        strip=True,
        label="Name",
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )


class RunnerConfirmForm(forms.Form):
    """A confirmed destructive action (new token, release, archive).

    The confirm dialog posts ``confirm=yes``; a bare POST without it (for
    example a replayed or forged request missing the field) is refused.
    """

    confirm = forms.CharField(max_length=8, widget=forms.HiddenInput)

    def clean_confirm(self) -> str:
        value: str = self.cleaned_data["confirm"]
        if value != "yes":
            raise forms.ValidationError("Confirm the action.")
        return value


class RunnerUpdateForm(forms.Form):
    """Runner detail page: update the connected Test Runner (moved from
    ``projects.forms``).

    ``expected_version`` is the server version the page showed; the service
    refuses it if the server has since been redeployed with another version.
    """

    expected_version = forms.CharField(
        max_length=MAX_VERSION_LENGTH, strip=True, widget=forms.HiddenInput
    )


class RunnerDisconnectForm(forms.Form):
    """Force disconnect; ``return_to`` picks the page to go back to."""

    RETURN_DETAIL = "detail"
    RETURN_ALL = "all"

    return_to = forms.ChoiceField(
        choices=[(RETURN_DETAIL, RETURN_DETAIL), (RETURN_ALL, RETURN_ALL)],
        required=False,
        widget=forms.HiddenInput,
    )
