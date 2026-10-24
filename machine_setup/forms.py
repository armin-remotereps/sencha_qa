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
