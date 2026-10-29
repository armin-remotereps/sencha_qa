"""Forms for the test session pages: start, stop and edit.

Lengths mirror ``TestSession.name`` / ``TestSession.note``; the services
store what the forms clean.
"""

from __future__ import annotations

from django import forms

SESSION_NAME_MAX_LENGTH = 120
SESSION_NOTE_MAX_LENGTH = 2000


def _name_field() -> forms.CharField:
    return forms.CharField(
        label="Name",
        required=False,
        max_length=SESSION_NAME_MAX_LENGTH,
        help_text="Leave empty to name it after the start time.",
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
    )


def _note_field(help_text: str) -> forms.CharField:
    return forms.CharField(
        label="Note",
        required=False,
        max_length=SESSION_NOTE_MAX_LENGTH,
        help_text=help_text,
        widget=forms.Textarea(attrs={"class": "textarea", "rows": 3}),
    )


class TestSessionStartForm(forms.Form):
    """Start a session: an optional name and note."""

    name = _name_field()
    note = _note_field("What you plan to test, for your own records.")


class TestSessionStopForm(forms.Form):
    """Stop the open session; an empty note keeps the current one."""

    note = _note_field("Leave empty to keep the current note.")


class TestSessionEditForm(forms.Form):
    """Rename a session or change its note."""

    name = _name_field()
    note = _note_field("For your own records.")
