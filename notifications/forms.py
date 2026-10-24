from __future__ import annotations

from typing import Any

from django import forms

from notifications.models import PUSH_ENDPOINT_MAX_LENGTH

# Base64url P-256 public key (65 bytes -> 87 chars) and 16-byte auth secret
# (22 chars); a little slack for padding.
PUSH_KEY_MAX_LENGTH = 255


class PushSubscriptionForm(forms.Form):
    """A browser's PushSubscription, posted by the pushDevice component."""

    endpoint = forms.URLField(
        max_length=PUSH_ENDPOINT_MAX_LENGTH, assume_scheme="https"
    )
    p256dh = forms.RegexField(
        regex=r"^[A-Za-z0-9_\-=]+$", max_length=PUSH_KEY_MAX_LENGTH
    )
    auth = forms.RegexField(regex=r"^[A-Za-z0-9_\-=]+$", max_length=PUSH_KEY_MAX_LENGTH)


class PushUnsubscribeForm(forms.Form):
    """Remove a device: by endpoint (this browser, via fetch) or by id (the list)."""

    endpoint = forms.URLField(
        max_length=PUSH_ENDPOINT_MAX_LENGTH, required=False, assume_scheme="https"
    )
    subscription_id = forms.IntegerField(min_value=1, required=False)

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        has_endpoint = bool(cleaned.get("endpoint"))
        has_id = cleaned.get("subscription_id") is not None
        if has_endpoint == has_id:
            raise forms.ValidationError("Give exactly one of endpoint or device.")
        return cleaned
