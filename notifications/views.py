from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from notifications.exceptions import NotificationError
from notifications.forms import NotificationKindsForm
from notifications.services.ntfy_config import ntfy_configured
from notifications.services.preferences import (
    disable,
    enable,
    get_preference_view,
    regenerate_topic,
    update_kinds,
)
from notifications.services.test_notification import send_test_notification


@login_required
@require_GET
def notification_settings(request: AuthenticatedRequest) -> HttpResponse:
    # Own user only: the preference view holds the user's secret topic.
    preference = get_preference_view(request.user)
    kinds_form = NotificationKindsForm(
        initial=(
            {
                "need_help": preference.notify_need_help,
                "failed": preference.notify_failed,
            }
            if preference is not None
            else {"need_help": True, "failed": True}
        )
    )
    return render(
        request,
        "notifications/settings.html",
        {
            "active_nav": "notifications",
            "ntfy_configured": ntfy_configured(),
            "preference": preference,
            "kinds_form": kinds_form,
        },
    )


@login_required
@require_POST
def enable_notifications(request: AuthenticatedRequest) -> HttpResponse:
    try:
        enable(request.user)
    except NotificationError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, "Notifications are on. Subscribe to your topic in ntfy."
        )
    return redirect("notifications:settings")


@login_required
@require_POST
def disable_notifications(request: AuthenticatedRequest) -> HttpResponse:
    disable(request.user)
    messages.success(request, "Notifications are off.")
    return redirect("notifications:settings")


@login_required
@require_POST
def regenerate_notification_topic(request: AuthenticatedRequest) -> HttpResponse:
    try:
        regenerate_topic(request.user)
    except NotificationError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, "Subscribe to the new topic in ntfy; the old one gets nothing."
        )
    return redirect("notifications:settings")


@login_required
@require_POST
def update_notification_kinds(request: AuthenticatedRequest) -> HttpResponse:
    form = NotificationKindsForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Those notification choices could not be saved.")
        return redirect("notifications:settings")
    try:
        update_kinds(
            request.user,
            need_help=form.cleaned_data["need_help"],
            failed=form.cleaned_data["failed"],
        )
    except NotificationError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(request, "Notification choices saved.")
    return redirect("notifications:settings")


@login_required
@require_POST
def test_notification(request: AuthenticatedRequest) -> HttpResponse:
    try:
        send_test_notification(request.user)
    except NotificationError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request, "Test notification sent. It should arrive in a few seconds."
        )
    return redirect("notifications:settings")
