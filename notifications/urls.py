from __future__ import annotations

from django.urls import URLPattern, path

from notifications import views

app_name = "notifications"

# Included at the site root: the service worker must be served from "/" to
# control every page, and the manifest sits beside it.
urlpatterns: list[URLPattern] = [
    path("sw.js", views.service_worker, name="service_worker"),
    path("manifest.webmanifest", views.web_app_manifest, name="manifest"),
    path("notifications/", views.notification_settings, name="settings"),
    path("notifications/subscriptions/", views.subscribe, name="subscribe"),
    path(
        "notifications/subscriptions/remove/",
        views.unsubscribe,
        name="unsubscribe",
    ),
    path("notifications/test/", views.test_notification, name="test"),
]
