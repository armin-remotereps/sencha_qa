from __future__ import annotations

from django.urls import URLPattern, path

from notifications import views

app_name = "notifications"

urlpatterns: list[URLPattern] = [
    path("notifications/", views.notification_settings, name="settings"),
    path("notifications/enable/", views.enable_notifications, name="enable"),
    path("notifications/disable/", views.disable_notifications, name="disable"),
    path(
        "notifications/regenerate/",
        views.regenerate_notification_topic,
        name="regenerate",
    ),
    path("notifications/kinds/", views.update_notification_kinds, name="kinds"),
    path("notifications/test/", views.test_notification, name="test"),
    path(
        "notifications/inbox/failures/<int:item_id>/dismiss/",
        views.dismiss_failure,
        name="dismiss_failure",
    ),
    path(
        "notifications/inbox/replies/<int:item_id>/dismiss/",
        views.dismiss_reply,
        name="dismiss_reply",
    ),
    path(
        "notifications/inbox/dismiss-all/",
        views.dismiss_all_inbox,
        name="dismiss_all",
    ),
]
