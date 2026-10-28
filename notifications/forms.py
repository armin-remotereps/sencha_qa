from __future__ import annotations

from django import forms

from notifications.services.dismiss_inbox import InboxKind


class NotificationKindsForm(forms.Form):
    """Which kinds of notification the user wants (Notifications page)."""

    need_help = forms.BooleanField(required=False, label="A case needs help")
    failed = forms.BooleanField(required=False, label="A case failed")
    chat_reply = forms.BooleanField(required=False, label="Chat replies")
    run_finished = forms.BooleanField(required=False, label="A run finished")
    night_digest = forms.BooleanField(required=False, label="Night run digest")


class InboxDismissAllForm(forms.Form):
    """Dismiss all on one inbox list, up to when the page was rendered."""

    kind = forms.ChoiceField(choices=InboxKind.choices)
    # The page's render time (ISO 8601): items that arrived later stay.
    up_to = forms.DateTimeField()
    # Optional same-site path to return to; checked in the view.
    next = forms.CharField(required=False, max_length=2000)
