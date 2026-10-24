from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET, require_POST

from accounts.types import AuthenticatedRequest
from notifications.exceptions import NotificationError
from notifications.forms import PushSubscriptionForm, PushUnsubscribeForm
from notifications.services.push_config import push_configured, vapid_public_key
from notifications.services.subscriptions import (
    delete_subscription_by_endpoint,
    delete_subscription_by_id,
    list_user_devices,
    save_subscription,
)
from notifications.services.test_push import send_test_push

INVALID_SUBSCRIPTION_MESSAGE = "This browser sent an invalid push subscription."


@login_required
@require_GET
def notification_settings(request: AuthenticatedRequest) -> HttpResponse:
    return render(
        request,
        "notifications/settings.html",
        {
            "active_nav": "notifications",
            "push_configured": push_configured(),
            "vapid_public_key": vapid_public_key(),
            "devices": list_user_devices(request.user),
        },
    )


@login_required
@require_POST
def subscribe(request: AuthenticatedRequest) -> HttpResponse:
    form = PushSubscriptionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"error": INVALID_SUBSCRIPTION_MESSAGE}, status=400)
    try:
        save_subscription(
            user=request.user,
            endpoint=form.cleaned_data["endpoint"],
            p256dh=form.cleaned_data["p256dh"],
            auth=form.cleaned_data["auth"],
            user_agent=request.headers.get("User-Agent", ""),
        )
    except NotificationError as exc:
        return JsonResponse({"error": exc.message}, status=400)
    return HttpResponse(status=204)


@login_required
@require_POST
def unsubscribe(request: AuthenticatedRequest) -> HttpResponse:
    form = PushUnsubscribeForm(request.POST)
    if not form.is_valid():
        if "subscription_id" in request.POST:
            messages.error(request, "That device could not be removed.")
            return redirect("notifications:settings")
        return JsonResponse({"error": INVALID_SUBSCRIPTION_MESSAGE}, status=400)

    subscription_id: int | None = form.cleaned_data["subscription_id"]
    if subscription_id is None:
        # fetch() from this browser's pushDevice component.
        delete_subscription_by_endpoint(
            user=request.user, endpoint=form.cleaned_data["endpoint"]
        )
        return HttpResponse(status=204)

    if delete_subscription_by_id(user=request.user, subscription_id=subscription_id):
        messages.success(request, "Device removed. It no longer gets notifications.")
    else:
        messages.error(request, "That device was already removed.")
    return redirect("notifications:settings")


@login_required
@require_POST
def test_notification(request: AuthenticatedRequest) -> HttpResponse:
    try:
        devices = send_test_push(request.user)
    except NotificationError as exc:
        messages.error(request, exc.message)
    else:
        messages.success(
            request,
            f"Test notification sent to {devices} device{'s' if devices != 1 else ''}.",
        )
    return redirect("notifications:settings")


@require_GET
@cache_control(no_cache=True, max_age=0)
def service_worker(request: HttpRequest) -> HttpResponse:
    response = render(
        request,
        "notifications/sw.js",
        content_type="application/javascript; charset=utf-8",
    )
    # Lets the worker at /sw.js control the whole site.
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
@cache_control(max_age=3600)
def web_app_manifest(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "notifications/manifest.webmanifest",
        content_type="application/manifest+json",
    )
