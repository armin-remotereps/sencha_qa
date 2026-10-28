from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from notifications.exceptions import InboxItemNotFoundError, NotificationError
from notifications.forms import InboxDismissAllForm, NotificationKindsForm
from notifications.services.dismiss_inbox import (
    InboxKind,
    dismiss_all,
    dismiss_missed_failure,
    dismiss_missed_reply,
)
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
                "chat_reply": preference.notify_chat_reply,
                "run_finished": preference.notify_run_finished,
                "night_digest": preference.notify_night_digest,
            }
            if preference is not None
            else {
                "need_help": True,
                "failed": True,
                "chat_reply": True,
                "run_finished": True,
                "night_digest": True,
            }
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
            chat_reply=form.cleaned_data["chat_reply"],
            run_finished=form.cleaned_data["run_finished"],
            night_digest=form.cleaned_data["night_digest"],
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


def _inbox_redirect(request: AuthenticatedRequest, next_url: str) -> HttpResponse:
    # Only same-site paths: anything else falls back to the dashboard.
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("dashboard:index")


@login_required
@require_POST
def dismiss_failure(request: AuthenticatedRequest, item_id: int) -> HttpResponse:
    try:
        dismiss_missed_failure(request.user, item_id)
    except InboxItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    return _inbox_redirect(request, request.POST.get("next", ""))


@login_required
@require_POST
def dismiss_reply(request: AuthenticatedRequest, item_id: int) -> HttpResponse:
    try:
        dismiss_missed_reply(request.user, item_id)
    except InboxItemNotFoundError as exc:
        raise Http404(exc.message) from exc
    return _inbox_redirect(request, request.POST.get("next", ""))


@login_required
@require_POST
def dismiss_all_inbox(request: AuthenticatedRequest) -> HttpResponse:
    form = InboxDismissAllForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Those items could not be dismissed. Reload the page.")
        return _inbox_redirect(request, request.POST.get("next", ""))
    dismissed = dismiss_all(
        request.user,
        InboxKind(form.cleaned_data["kind"]),
        up_to=form.cleaned_data["up_to"],
    )
    messages.success(
        request, f"{dismissed} item{'' if dismissed == 1 else 's'} dismissed."
    )
    return _inbox_redirect(request, form.cleaned_data["next"])
