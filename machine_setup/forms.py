from __future__ import annotations

from django import forms

SETUP_MESSAGE_MAX_LENGTH = 8000


class SetupMessageForm(forms.Form):
    """One chat message to the machine setup agent."""

    text = forms.CharField(
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=True,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    # The thread replied in; empty for the main chat.
    conversation_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class SetupThreadForm(forms.Form):
    """The thread a Stop was pressed in, so the page returns to it."""

    conversation_id = forms.IntegerField(
        required=False, min_value=1, widget=forms.HiddenInput
    )


class OpenHelpChatForm(forms.Form):
    """Open the help chat of a case that needs help, with an optional first message."""

    text = forms.CharField(
        label="First message",
        max_length=SETUP_MESSAGE_MAX_LENGTH,
        strip=True,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "textarea",
                "rows": 3,
                "placeholder": (
                    "Optional: what you want to talk about. Empty asks the agent "
                    "to look at the machine and suggest what to do."
                ),
            }
        ),
    )
