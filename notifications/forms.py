from __future__ import annotations

from django import forms


class NotificationKindsForm(forms.Form):
    """Which kinds of notification the user wants (Notifications page)."""

    need_help = forms.BooleanField(required=False, label="A case needs help")
    failed = forms.BooleanField(required=False, label="A case failed")
    chat_reply = forms.BooleanField(required=False, label="Chat replies")
